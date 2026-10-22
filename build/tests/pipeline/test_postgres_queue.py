"""Live Postgres tests for SPEC-09's queue primitives and SPEC-11's fairness.

Run with DATABASE_URL set to the migrator DSN; the module skips otherwise. Every
test derives a unique provider_repo_id, matching the contract suite's rule for a
globally unique repo identity.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from buildplane.pipeline.queue import PostgresJobQueue
from buildplane.queue.reconcile import PostgresQueueReconciler

DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _repo_id() -> int:
    return (uuid.uuid4().int % 9_000_000_000) + 1_000_000_000


async def _setup(
    conn,
    *,
    status: str = "pending",
    created_at: datetime = NOW,
    available_at: datetime = NOW,
):
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    job_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:10]
    await conn.execute(
        "INSERT INTO orgs (id, name, slug) VALUES ($1, $2, $3)",
        org_id,
        f"queue-{tag}",
        f"queue-{tag}",
    )
    await conn.execute(
        """
        INSERT INTO projects
            (id, org_id, name, slug, provider, provider_repo_id, k8s_namespace)
        VALUES ($1, $2, $3, $4, 'github', $5, $6)
        """,
        project_id,
        org_id,
        f"project-{tag}",
        f"repo-{tag}",
        _repo_id(),
        f"ns-{tag}",
    )
    await conn.execute(
        """
        INSERT INTO deployments
            (id, project_id, org_id, git_sha, git_ref, environment,
             trigger_source, status, build_env_keys, created_at)
        VALUES ($1, $2, $3, $4, 'refs/heads/main', 'production',
                'webhook', 'queued', '{}'::text[], $5)
        """,
        deployment_id,
        project_id,
        org_id,
        "a" * 40,
        created_at,
    )
    await conn.execute(
        """
        INSERT INTO build_jobs
            (id, deployment_id, org_id, project_id, status,
             available_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $7)
        """,
        job_id,
        deployment_id,
        org_id,
        project_id,
        status,
        available_at,
        created_at,
    )
    return org_id, project_id, deployment_id, job_id


async def _add_project(conn, org_id):
    project_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:10]
    await conn.execute(
        """
        INSERT INTO projects
            (id, org_id, name, slug, provider, provider_repo_id, k8s_namespace)
        VALUES ($1, $2, $3, $4, 'github', $5, $6)
        """,
        project_id,
        org_id,
        f"project-{tag}",
        f"repo-{tag}",
        _repo_id(),
        f"ns-{tag}",
    )
    return project_id


async def _add_queued_job(
    conn,
    *,
    org_id,
    project_id,
    sha: str,
    created_at: datetime = NOW,
    available_at: datetime = NOW,
    git_ref: str = "refs/heads/main",
    priority: int = 0,
):
    deployment_id = uuid.uuid4()
    job_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO deployments
            (id, project_id, org_id, git_sha, git_ref, environment,
             trigger_source, status, build_env_keys, created_at)
        VALUES ($1, $2, $3, $4, $5, 'production', 'webhook', 'queued',
                '{}'::text[], $6)
        """,
        deployment_id,
        project_id,
        org_id,
        sha,
        git_ref,
        created_at,
    )
    await conn.execute(
        """
        INSERT INTO build_jobs
            (id, deployment_id, org_id, project_id, status, priority,
             available_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7, $7)
        """,
        job_id,
        deployment_id,
        org_id,
        project_id,
        priority,
        available_at,
        created_at,
    )
    return deployment_id, job_id


async def _set_plan(conn, org_id, code: str = "pro"):
    result = await conn.execute(
        "UPDATE orgs SET plan_id = (SELECT id FROM plans WHERE code = $1) "
        "WHERE id = $2",
        code,
        org_id,
    )
    assert result == "UPDATE 1"


async def _cleanup(conn, org_id):
    await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)


def test_claim_heartbeat_and_lease_sweep_round_trip():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn, lease_seconds=60, heartbeat_seconds=15)

            claim = await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            )
            assert claim is not None
            assert claim.job_id == str(job_id)
            assert claim.deployment_id == str(deployment_id)
            assert claim.project_id == str(project_id)
            assert claim.project_name.startswith("project-")
            assert claim.attempts == 0
            assert claim.build_env_keys == ()
            assert claim.manifest is None
            assert claim.lease_expires_at == NOW + timedelta(seconds=60)

            # A second worker cannot claim the same job. The default free-plan
            # cap is 1, so it also must not claim a second job for this org.
            assert await queue.claim(
                "worker-b", now=NOW, org_ids=(str(org_id),)
            ) is None

            job = await conn.fetchrow(
                "SELECT status, worker_id, lease_expires_at FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert job["status"] == "running"
            assert job["worker_id"] == "worker-a"
            deployment = await conn.fetchrow(
                "SELECT status, stage, started_at FROM deployments WHERE id = $1",
                deployment_id,
            )
            assert deployment["status"] == "building"
            assert deployment["stage"] == "fetch"
            assert deployment["started_at"] == NOW

            assert await queue.heartbeat(
                str(job_id), "worker-a", now=NOW + timedelta(seconds=10)
            )
            assert not await queue.heartbeat(
                str(job_id), "worker-b", now=NOW + timedelta(seconds=10)
            )
            beat = await conn.fetchrow(
                "SELECT heartbeat_at, lease_expires_at FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert beat["heartbeat_at"] == NOW + timedelta(seconds=10)
            assert beat["lease_expires_at"] == NOW + timedelta(seconds=70)

            swept = await queue.sweep_expired(now=NOW + timedelta(seconds=120))
            assert swept == ((str(job_id), str(deployment_id)),)
            job = await conn.fetchrow(
                "SELECT status, worker_id, lease_expires_at, last_error "
                "FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert job["status"] == "pending"
            assert job["worker_id"] is None
            assert job["lease_expires_at"] is None
            assert job["last_error"] == "lease_expired"
            deployment = await conn.fetchrow(
                "SELECT status, stage, started_at FROM deployments WHERE id = $1",
                deployment_id,
            )
            assert deployment["status"] == "queued"
            assert deployment["stage"] is None
            assert deployment["started_at"] is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_two_workers_claim_different_jobs_without_blocking():
    async def scenario():
        conn_a = await asyncpg.connect(DSN)
        conn_b = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, _deployment_id, _job_id = await _setup(conn_a)
            await _set_plan(conn_a, org_id, "pro")
            second_deployment, second_job = await _add_queued_job(
                conn_a,
                org_id=org_id,
                project_id=project_id,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
            )
            queue_a = PostgresJobQueue(conn_a, lease_seconds=60, heartbeat_seconds=15)
            queue_b = PostgresJobQueue(conn_b, lease_seconds=60, heartbeat_seconds=15)
            first, second = await asyncio.gather(
                queue_a.claim("worker-a", now=NOW, org_ids=(str(org_id),)),
                queue_b.claim("worker-b", now=NOW, org_ids=(str(org_id),)),
            )
            assert first is not None and second is not None
            assert first.job_id != second.job_id
            assert {first.deployment_id, second.deployment_id} == {
                str(_deployment_id),
                str(second_deployment),
            }
            assert {first.job_id, second.job_id} == {str(_job_id), str(second_job)}
        finally:
            if org_id is not None:
                await _cleanup(conn_a, org_id)
            await conn_b.close()
            await conn_a.close()

    asyncio.run(scenario())


def test_a_sigkilled_worker_job_is_requeued_within_the_lease_window():
    async def scenario():
        conn_a = await asyncpg.connect(DSN)
        conn_b = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, job_id = await _setup(conn_a)
            claim = await PostgresJobQueue(
                conn_a, lease_seconds=60, heartbeat_seconds=15
            ).claim("worker-a", now=NOW, org_ids=(str(org_id),))
            assert claim is not None

            # The worker process/connection disappears without releasing.
            await conn_a.close()

            queue_b = PostgresJobQueue(
                conn_b, lease_seconds=60, heartbeat_seconds=15
            )
            swept = await queue_b.sweep_expired(
                now=NOW + timedelta(seconds=61)
            )
            assert swept == ((str(job_id), str(deployment_id)),)
            job = await conn_b.fetchrow(
                "SELECT status, worker_id, lease_expires_at FROM build_jobs "
                "WHERE id = $1",
                job_id,
            )
            assert job["status"] == "pending"
            assert job["worker_id"] is None
            assert job["lease_expires_at"] is None

            reclaimed = await queue_b.claim(
                "worker-b",
                now=NOW + timedelta(seconds=61),
                org_ids=(str(org_id),),
            )
            assert reclaimed is not None
            assert reclaimed.job_id == str(job_id)
        finally:
            if org_id is not None:
                await _cleanup(conn_b, org_id)
            await conn_b.close()

    asyncio.run(scenario())


def test_release_returns_owned_job_to_pending_without_finishing_it():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn, lease_seconds=60, heartbeat_seconds=15)
            await queue.claim("worker-a", now=NOW, org_ids=(str(org_id),))
            assert await queue.release(
                str(job_id),
                "worker-a",
                reason="transient infrastructure",
                now=NOW,
            )
            assert not await queue.release(
                str(job_id), "worker-b", reason="not owner", now=NOW
            )
            job = await conn.fetchrow(
                "SELECT status, last_error FROM build_jobs WHERE id = $1", job_id
            )
            assert job["status"] == "pending"
            assert job["last_error"] == "transient infrastructure"
            deployment = await conn.fetchrow(
                "SELECT status FROM deployments WHERE id = $1", deployment_id
            )
            assert deployment["status"] == "queued"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_cancelled_deployment_job_is_cancelled_not_left_pending():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn)
            assert await queue.cancel_deployment(
                str(deployment_id), reason="cancelled by the user", now=NOW
            ) == 1
            assert await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            ) is None

            status = await conn.fetchval(
                "SELECT status FROM build_jobs WHERE id = $1", job_id
            )
            assert status == "cancelled"
            status = await conn.fetchval(
                "SELECT status FROM deployments WHERE id = $1", deployment_id
            )
            assert status == "cancelled"

            # A later lease sweep must not resurrect it as pending.
            assert await queue.sweep_expired(now=NOW + timedelta(minutes=5)) == ()
            status = await conn.fetchval(
                "SELECT status FROM build_jobs WHERE id = $1", job_id
            )
            assert status == "cancelled"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_cancelled_running_job_does_not_return_to_pending_on_sweep():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn)
            assert await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            ) is not None
            # Simulate a control-plane cancellation that reaches the job while
            # the worker is still running.
            assert await queue.cancel_deployment(
                str(deployment_id), reason="cancelled by the user", now=NOW
            ) == 1

            assert await queue.sweep_expired(
                now=NOW + timedelta(minutes=5)
            ) == ()
            job = await conn.fetchrow(
                "SELECT status, worker_id FROM build_jobs WHERE id = $1", job_id
            )
            assert job["status"] == "cancelled"
            assert job["worker_id"] is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_per_org_cap_is_across_all_projects_and_never_overshoots():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_one, _deployment_id, _job_id = await _setup(conn)
            await _set_plan(conn, org_id, "pro")
            project_two = await _add_project(conn, org_id)
            project_three = await _add_project(conn, org_id)

            # Four queued builds across the org's three projects.
            await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_one,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
            )
            await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_two,
                sha="c" * 40,
                created_at=NOW + timedelta(seconds=2),
            )
            await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_three,
                sha="d" * 40,
                created_at=NOW + timedelta(seconds=3),
            )

            queue = PostgresJobQueue(conn)
            claimed = [
                await queue.claim(
                    f"worker-{index}", now=NOW, org_ids=(str(org_id),)
                )
                for index in range(4)
            ]
            assert all(claim is not None for claim in claimed[:3])
            assert claimed[3] is None
            running = await conn.fetchval(
                "SELECT count(*) FROM build_jobs WHERE org_id = $1 "
                "AND status = 'running'",
                org_id,
            )
            assert running == 3
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_capped_org_is_skipped_so_another_org_goes_next():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_a = None
        org_b = None
        try:
            org_a, project_a, deployment_a, _job_a = await _setup(conn)
            await _add_queued_job(
                conn,
                org_id=org_a,
                project_id=project_a,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
            )
            org_b, _project_b, deployment_b, _job_b = await _setup(
                conn, created_at=NOW + timedelta(seconds=2)
            )

            queue = PostgresJobQueue(conn)
            scope = (str(org_a), str(org_b))
            first = await queue.claim("worker-a", now=NOW, org_ids=scope)
            second = await queue.claim("worker-b", now=NOW, org_ids=scope)
            assert first is not None and second is not None
            assert first.deployment_id == str(deployment_a)
            # org_a is at its free-plan cap after the first claim; its second
            # job must not jump ahead of org_b's older waiting work.
            assert second.deployment_id == str(deployment_b)
            assert await queue.claim("worker-c", now=NOW, org_ids=scope) is None
        finally:
            if org_a is not None:
                await _cleanup(conn, org_a)
            if org_b is not None:
                await _cleanup(conn, org_b)
            await conn.close()

    asyncio.run(scenario())


def test_explicit_priority_overrides_fifo_order():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, _deployment_id, _job_id = await _setup(conn)
            await _set_plan(conn, org_id, "pro")
            urgent_deployment, _urgent_job = await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_id,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
                priority=10,
            )
            queue = PostgresJobQueue(conn)
            claimed = await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            )
            assert claimed is not None
            assert claimed.deployment_id == str(urgent_deployment)
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_concurrent_builds_plan_key_is_accepted_as_a_fallback():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        plan_id = uuid.uuid4()
        tag = uuid.uuid4().hex[:10]
        try:
            org_id, project_id, _deployment_id, _job_id = await _setup(conn)
            await conn.execute(
                """
                INSERT INTO plans (id, code, name, limits)
                VALUES ($1, $2, $3, '{"concurrent_builds": 2}'::jsonb)
                """,
                plan_id,
                f"fallback-{tag}",
                f"Fallback {tag}",
            )
            await conn.execute(
                "UPDATE orgs SET plan_id = $1 WHERE id = $2", plan_id, org_id
            )
            await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_id,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
            )
            queue = PostgresJobQueue(conn)
            first = await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            )
            second = await queue.claim(
                "worker-b", now=NOW, org_ids=(str(org_id),)
            )
            third = await queue.claim(
                "worker-c", now=NOW, org_ids=(str(org_id),)
            )
            assert first is not None and second is not None
            assert third is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.execute("DELETE FROM plans WHERE id = $1", plan_id)
            await conn.close()

    asyncio.run(scenario())


def test_queue_position_and_depth_are_customer_visible():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, _deployment_id, job_id = await _setup(conn)
            second_deployment, second_job = await _add_queued_job(
                conn,
                org_id=org_id,
                project_id=project_id,
                sha="b" * 40,
                created_at=NOW + timedelta(seconds=1),
            )
            queue = PostgresJobQueue(conn)
            assert await queue.queue_depth(now=NOW, org_id=str(org_id)) == 2
            assert await queue.position(
                str(job_id), now=NOW, org_id=str(org_id)
            ) == 1
            assert await queue.position(
                str(second_job), now=NOW, org_id=str(org_id)
            ) == 2

            # Once claimed, the job no longer has a waiting position.
            assert await queue.claim(
                "worker-a", now=NOW, org_ids=(str(org_id),)
            ) is not None
            assert await queue.position(
                str(job_id), now=NOW, org_id=str(org_id)
            ) is None
            assert await queue.queue_depth(now=NOW, org_id=str(org_id)) == 1
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_reconciler_re_enqueues_an_orphaned_queued_deployment():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, _job_id = await _setup(conn)
            await conn.execute(
                "DELETE FROM build_jobs WHERE deployment_id = $1", deployment_id
            )
            reconciler = PostgresQueueReconciler(conn, clock=lambda: NOW)
            healed = await reconciler.reconcile(
                now=NOW, org_id=str(org_id)
            )
            assert len(healed) == 1
            assert healed[0].deployment_id == str(deployment_id)
            assert healed[0].org_id == str(org_id)

            job = await conn.fetchrow(
                "SELECT status, available_at FROM build_jobs "
                "WHERE deployment_id = $1",
                deployment_id,
            )
            assert job is not None
            assert job["status"] == "pending"
            assert job["available_at"] == NOW

            # Idempotent: the next pass finds nothing.
            assert await reconciler.reconcile(
                now=NOW, org_id=str(org_id)
            ) == ()
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())
