"""Live Postgres tests for SPEC-09's queue slice.

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

DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _repo_id() -> int:
    return (uuid.uuid4().int % 9_000_000_000) + 1_000_000_000


async def _setup(conn, *, status: str = "pending"):
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
        NOW,
    )
    await conn.execute(
        """
        INSERT INTO build_jobs
            (id, deployment_id, org_id, project_id, status,
             available_at, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5, $6, $6, $6)
        """,
        job_id,
        deployment_id,
        org_id,
        project_id,
        status,
        NOW,
    )
    return org_id, project_id, deployment_id, job_id


async def _cleanup(conn, org_id):
    await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)


def test_claim_heartbeat_and_lease_sweep_round_trip():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = project_id = deployment_id = job_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn, lease_seconds=60, heartbeat_seconds=15)

            claim = await queue.claim("worker-a", now=NOW)
            assert claim is not None
            assert claim.job_id == str(job_id)
            assert claim.deployment_id == str(deployment_id)
            assert claim.project_id == str(project_id)
            assert claim.project_name.startswith("project-")
            assert claim.attempts == 0
            assert claim.build_env_keys == ()
            assert claim.manifest is None
            assert claim.lease_expires_at == NOW + timedelta(seconds=60)

            # A second worker cannot claim the same job.
            assert await queue.claim("worker-b", now=NOW) is None

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
            second_deployment = uuid.uuid4()
            second_job = uuid.uuid4()
            await conn_a.execute(
                """
                INSERT INTO deployments
                    (id, project_id, org_id, git_sha, git_ref, environment,
                     trigger_source, status, build_env_keys, created_at)
                VALUES ($1, $2, $3, $4, 'refs/heads/main', 'production',
                        'webhook', 'queued', '{}'::text[], $5)
                """,
                second_deployment,
                project_id,
                org_id,
                "b" * 40,
                NOW,
            )
            await conn_a.execute(
                """
                INSERT INTO build_jobs
                    (id, deployment_id, org_id, project_id, status,
                     available_at, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'pending', $5, $5, $5)
                """,
                second_job,
                second_deployment,
                org_id,
                project_id,
                NOW,
            )
            queue_a = PostgresJobQueue(conn_a, lease_seconds=60, heartbeat_seconds=15)
            queue_b = PostgresJobQueue(conn_b, lease_seconds=60, heartbeat_seconds=15)
            first, second = await asyncio.gather(
                queue_a.claim("worker-a", now=NOW),
                queue_b.claim("worker-b", now=NOW),
            )
            assert first is not None and second is not None
            assert first.job_id != second.job_id
            assert {first.deployment_id, second.deployment_id} == {
                str(_deployment_id),
                str(second_deployment),
            }
        finally:
            if org_id is not None:
                await _cleanup(conn_a, org_id)
            await conn_b.close()
            await conn_a.close()

    asyncio.run(scenario())


def test_release_returns_owned_job_to_pending_without_finishing_it():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, job_id = await _setup(conn)
            queue = PostgresJobQueue(conn, lease_seconds=60, heartbeat_seconds=15)
            await queue.claim("worker-a", now=NOW)
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


def test_cancelled_deployment_is_never_claimed():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, _project_id, deployment_id, _job_id = await _setup(conn)
            await conn.execute(
                "UPDATE deployments SET status = 'cancelled', finished_at = $1 "
                "WHERE id = $2",
                NOW,
                deployment_id,
            )
            queue = PostgresJobQueue(conn)
            assert await queue.claim("worker-a", now=NOW) is None
            # The pending job is still pending; the claim predicate is what
            # protects a cancelled deployment, not a destructive side effect.
            status = await conn.fetchval(
                "SELECT status FROM build_jobs WHERE deployment_id = $1",
                deployment_id,
            )
            assert status == "pending"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())
