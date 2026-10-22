"""Live Postgres tests for SPEC-09 result reporting."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
from platform_shared.manifest import canonical_manifest_json

from buildplane.pipeline.models import BuildOutcome, Stage
from buildplane.pipeline.reporting import PostgresBuildReporter
from buildplane.pipeline.queue import PostgresJobQueue
from support import builder_ref, make_claim

DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
MANIFEST = {
    "version": 1,
    "name": "demo",
    "services": [
        {
            "name": "web",
            "source": ".",
            "type": "static",
            "build": {"strategy": "none", "outputDir": "."},
            "runtime": {"port": 3000},
        }
    ],
}
CANONICAL, MANIFEST_HASH = canonical_manifest_json(MANIFEST)


def _repo_id() -> int:
    return (uuid.uuid4().int % 9_000_000_000) + 1_000_000_000


async def _setup(conn, *, status: str = "building", job_status: str = "running"):
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    job_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:10]
    await conn.execute(
        "INSERT INTO orgs (id, name, slug) VALUES ($1, $2, $3)",
        org_id,
        f"report-{tag}",
        f"report-{tag}",
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
             trigger_source, status, build_env_keys, started_at, created_at,
             finished_at)
        VALUES ($1, $2, $3, $4, 'refs/heads/main', 'production',
                'webhook', $5, '{API_KEY}'::text[], $6, $6, $7)
        """,
        deployment_id,
        project_id,
        org_id,
        "a" * 40,
        status,
        NOW,
        NOW if status == "cancelled" else None,
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
        job_status,
        NOW,
    )
    return org_id, project_id, deployment_id, job_id


async def _cleanup(conn, org_id):
    await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)


def _claim(org_id, project_id, deployment_id, job_id, **overrides):
    values = dict(
        job_id=str(job_id),
        deployment_id=str(deployment_id),
        org_id=str(org_id),
        project_id=str(project_id),
        build_env_keys=("API_KEY",),
        git_sha="a" * 40,
    )
    values.update(overrides)
    return make_claim(**values)


def test_success_records_all_inputs_but_does_not_promote_to_ready():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=True,
                stage=Stage.BUILD,
                exit_code=0,
                manifest=MANIFEST,
                manifest_hash=MANIFEST_HASH,
                build_image=builder_ref("static"),
                log_uri="file:///tmp/build.log",
                cache_key="sha256:" + "b" * 64,
                cache_hit=False,
                output_hash="sha256:" + "c" * 64,
                node_version=20,
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_success(claim, outcome)

            job = await conn.fetchrow(
                "SELECT status, lease_expires_at FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert job["status"] == "succeeded"
            assert job["lease_expires_at"] is None
            row = await conn.fetchrow(
                """
                SELECT status, stage, manifest, manifest_hash, build_image,
                       build_env_keys, node_version, exit_code, log_uri,
                       error_message, finished_at
                FROM deployments WHERE id = $1
                """,
                deployment_id,
            )
            assert row["status"] == "building"
            assert row["stage"] == "built"
            assert json.loads(row["manifest"]) == MANIFEST
            assert row["manifest_hash"] == MANIFEST_HASH
            assert row["build_image"] == builder_ref("static")
            assert row["build_env_keys"] == ["API_KEY"]
            assert row["node_version"] == 20
            assert row["exit_code"] == 0
            assert row["log_uri"] == "file:///tmp/build.log"
            assert row["error_message"] is None
            assert row["finished_at"] is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_transient_failure_requeues_with_backoff_and_attempt_increment():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=False,
                stage=Stage.FETCH,
                exit_code=None,
                retryable=True,
                error="provider is down",
                error_code="fetch_failed",
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_failure(claim, outcome)

            job = await conn.fetchrow(
                """
                SELECT status, attempts, available_at, last_error,
                       worker_id, lease_expires_at
                FROM build_jobs WHERE id = $1
                """,
                job_id,
            )
            assert job["status"] == "pending"
            assert job["attempts"] == 1
            assert job["available_at"] == NOW + timedelta(seconds=5)
            assert job["last_error"] == "fetch_failed: provider is down"
            assert job["worker_id"] is None
            assert job["lease_expires_at"] is None
            row = await conn.fetchrow(
                """
                SELECT status, stage, exit_code, error_message, log_uri,
                       started_at, finished_at
                FROM deployments WHERE id = $1
                """,
                deployment_id,
            )
            assert row["status"] == "queued"
            assert row["stage"] == "fetch"
            assert row["exit_code"] is None
            assert row["error_message"] == "provider is down"
            assert row["log_uri"] == "file:///tmp/build.log"
            assert row["started_at"] is None
            assert row["finished_at"] is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_exhausted_retries_move_job_to_the_inspectable_dlq():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            await conn.execute(
                "UPDATE build_jobs SET attempts = 2 WHERE id = $1", job_id
            )
            claim = _claim(
                org_id,
                project_id,
                deployment_id,
                job_id,
                attempts=2,
                max_attempts=3,
            )
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=False,
                stage=Stage.FETCH,
                exit_code=None,
                retryable=True,
                error="provider is still down",
                error_code="fetch_failed",
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_failure(claim, outcome)

            job = await conn.fetchrow(
                "SELECT status, attempts, last_error FROM build_jobs "
                "WHERE id = $1",
                job_id,
            )
            assert job["status"] == "dead"
            assert job["attempts"] == 3
            assert job["last_error"] == "fetch_failed: provider is still down"
            deployment = await conn.fetchrow(
                "SELECT status, finished_at FROM deployments WHERE id = $1",
                deployment_id,
            )
            assert deployment["status"] == "failed"
            assert deployment["finished_at"] == NOW

            queue = PostgresJobQueue(conn)
            letters = await queue.dead_letters(org_id=str(org_id))
            assert len(letters) == 1
            letter = letters[0]
            assert letter.job_id == str(job_id)
            assert letter.deployment_id == str(deployment_id)
            assert letter.attempts == 3
            assert letter.max_attempts == 3
            assert letter.last_error == "fetch_failed: provider is still down"
            assert letter.deployment_status == "failed"
            assert await queue.dead_letter(str(job_id)) == letter
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_cancellation_beats_a_retryable_failure():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(
                conn, status="cancelled", job_status="running"
            )
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=False,
                stage=Stage.BUILD,
                exit_code=1,
                retryable=True,
                error="worker failed after cancellation",
                error_code="sandbox_failed",
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_failure(claim, outcome)

            job = await conn.fetchrow(
                "SELECT status, last_error, available_at FROM build_jobs "
                "WHERE id = $1",
                job_id,
            )
            assert job["status"] == "cancelled"
            assert job["last_error"] == "cancelled"
            assert job["available_at"] == NOW
            deployment = await conn.fetchval(
                "SELECT status FROM deployments WHERE id = $1", deployment_id
            )
            assert deployment == "cancelled"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_late_failure_does_not_rewrite_a_requeued_job():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(
                conn, status="queued", job_status="pending"
            )
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=False,
                stage=Stage.BUILD,
                exit_code=1,
                retryable=False,
                error="late worker failure",
                error_code="sandbox_failed",
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_failure(claim, outcome)

            assert await conn.fetchval(
                "SELECT status FROM build_jobs WHERE id = $1", job_id
            ) == "pending"
            assert await conn.fetchval(
                "SELECT status FROM deployments WHERE id = $1", deployment_id
            ) == "queued"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_cancelled_deployment_is_not_resurrected_by_a_late_success():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(
                conn, status="cancelled", job_status="running"
            )
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=True,
                stage=Stage.BUILD,
                exit_code=0,
                manifest=MANIFEST,
                manifest_hash=MANIFEST_HASH,
                build_image=builder_ref("static"),
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_success(claim, outcome)

            job = await conn.fetchrow(
                "SELECT status, last_error FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert job["status"] == "cancelled"
            assert job["last_error"] == "cancelled"
            deployment = await conn.fetchrow(
                "SELECT status, manifest, stage FROM deployments WHERE id = $1",
                deployment_id,
            )
            assert deployment["status"] == "cancelled"
            assert deployment["manifest"] is None
            assert deployment["stage"] is None
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())


def test_permanent_failure_marks_job_and_deployment_terminal():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        try:
            org_id, project_id, deployment_id, job_id = await _setup(conn)
            claim = _claim(org_id, project_id, deployment_id, job_id)
            outcome = BuildOutcome(
                job_id=str(job_id),
                deployment_id=str(deployment_id),
                success=False,
                stage=Stage.BUILD,
                exit_code=7,
                retryable=False,
                error="build container exited with code 7",
                error_code="sandbox_failed",
                log_uri="file:///tmp/build.log",
            )
            reporter = PostgresBuildReporter(conn, clock=lambda: NOW)
            await reporter.report_failure(claim, outcome)

            job = await conn.fetchrow(
                "SELECT status, last_error FROM build_jobs WHERE id = $1",
                job_id,
            )
            assert job["status"] == "failed"
            assert job["last_error"] == (
                "sandbox_failed: build container exited with code 7"
            )
            row = await conn.fetchrow(
                """
                SELECT status, stage, exit_code, error_message, finished_at
                FROM deployments WHERE id = $1
                """,
                deployment_id,
            )
            assert row["status"] == "failed"
            assert row["stage"] == "build"
            assert row["exit_code"] == 7
            assert row["error_message"] == "build container exited with code 7"
            assert row["finished_at"] == NOW
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id)
            await conn.close()

    asyncio.run(scenario())
