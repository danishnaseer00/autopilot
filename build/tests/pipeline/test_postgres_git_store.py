"""Live Postgres tests for the SPEC-08 persistence adapter."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest

from buildplane.gitware.models import (
    DeploymentIntent,
    Environment,
    ProjectStatus,
    VerificationMethod,
    WebhookEventRecord,
)
from buildplane.gitware.postgres_store import PostgresGitStore
from buildplane.queue.admission import (
    PostgresQueueAdmission,
    QueueAdmissionPolicy,
)
from buildplane.queue.errors import QueueFull, QueueUnavailable
from buildplane.queue.reconcile import PostgresQueueReconciler

DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _repo_id() -> int:
    return (uuid.uuid4().int % 9_000_000_000) + 1_000_000_000


async def _setup(conn):
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    tag = uuid.uuid4().hex[:10]
    await conn.execute(
        "INSERT INTO orgs (id, name, slug) VALUES ($1, $2, $3)",
        org_id,
        f"git-{tag}",
        f"git-{tag}",
    )
    await conn.execute(
        """
        INSERT INTO projects
            (id, org_id, name, slug, provider, provider_repo_id,
             default_branch, k8s_namespace, verification_token)
        VALUES ($1, $2, $3, $4, 'github', $5, 'main', $6, 'verify-me')
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
        INSERT INTO project_branches
            (project_id, org_id, git_ref, environment, auto_deploy, position)
        VALUES ($1, $2, 'refs/heads/main', 'production', true, 1),
               ($1, $2, '*', 'preview', true, 0)
        """,
        project_id,
        org_id,
    )
    return org_id, project_id


async def _add_project(conn, org_id, tag: str):
    project_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO projects
            (id, org_id, name, slug, provider, provider_repo_id, k8s_namespace)
        VALUES ($1, $2, $3, $4, 'github', $5, $6)
        """,
        project_id,
        org_id,
        f"other-{tag}",
        f"other-repo-{tag}",
        _repo_id(),
        f"other-ns-{tag}",
    )
    return project_id


async def _cleanup(conn, org_id, delivery_prefix):
    await conn.execute(
        "DELETE FROM webhook_deliveries WHERE delivery_id LIKE $1",
        f"{delivery_prefix}%",
    )
    await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)


def _intent(project_id, org_id, *, sha: str, delivery_id: str, ref="refs/heads/main"):
    return DeploymentIntent(
        project_id=str(project_id),
        org_id=str(org_id),
        provider="github",
        provider_repo_id=_repo_id(),
        git_sha=sha,
        git_ref=ref,
        environment=Environment.PRODUCTION,
        delivery_id=delivery_id,
    )


def test_project_reads_branch_order_and_verification_mapping():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)
            store = PostgresGitStore(conn)

            project = await store.find_project(
                "github",
                await conn.fetchval(
                    "SELECT provider_repo_id FROM projects WHERE id = $1",
                    project_id,
                ),
            )
            assert project is not None
            assert project.id == str(project_id)
            assert project.status is ProjectStatus.ACTIVE
            assert project.verified is False
            assert project.verification_token == "verify-me"

            rules = await store.list_branch_rules(str(project_id))
            assert [rule.git_ref for rule in rules] == [
                "*",
                "refs/heads/main",
            ]

            await store.mark_verified(
                str(project_id), VerificationMethod.DEPLOY_BRANCH, at=NOW
            )
            stored = await conn.fetchrow(
                "SELECT verification_method, verified_at FROM projects WHERE id = $1",
                project_id,
            )
            assert stored["verification_method"] == "branch"
            verified = await store.get_project(str(project_id))
            assert verified is not None
            assert verified.verified is True
            assert verified.verification_method is VerificationMethod.DEPLOY_BRANCH
            assert verified.verified_at == NOW

            await store.mark_unverified(
                str(project_id), reason="token missing", at=NOW
            )
            unverified = await store.get_project(str(project_id))
            assert unverified is not None
            assert unverified.verified is False
            assert unverified.verification_error == "token missing"

            await store.set_project_status(
                str(project_id), ProjectStatus.ORPHANED
            )
            orphaned = await store.get_project(str(project_id))
            assert orphaned is not None
            assert orphaned.status is ProjectStatus.ORPHANED
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_record_intent_is_idempotent_and_supersedes_queued_work():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)
            store = PostgresGitStore(conn)
            first = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="a" * 40,
                    delivery_id=f"{prefix}-one",
                ),
                received_at=NOW,
            )
            assert first.created is True
            assert first.superseded == ()

            duplicate = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="a" * 40,
                    delivery_id=f"{prefix}-duplicate",
                ),
                received_at=NOW,
            )
            assert duplicate.created is False
            assert duplicate.deployment_id == first.deployment_id
            assert await conn.fetchval(
                "SELECT count(*) FROM deployments WHERE project_id = $1",
                project_id,
            ) == 1

            deployment = await conn.fetchrow(
                "SELECT status, build_env_keys, manifest, build_image "
                "FROM deployments WHERE id = $1",
                uuid.UUID(first.deployment_id),
            )
            assert deployment["status"] == "queued"
            assert deployment["build_env_keys"] == []
            assert deployment["manifest"] is None
            assert deployment["build_image"] is None
            job = await conn.fetchrow(
                "SELECT status FROM build_jobs WHERE deployment_id = $1",
                uuid.UUID(first.deployment_id),
            )
            assert job["status"] == "pending"

            newer = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="b" * 40,
                    delivery_id=f"{prefix}-two",
                ),
                received_at=NOW,
            )
            assert newer.created is True
            assert newer.superseded == (first.deployment_id,)
            old = await conn.fetchrow(
                "SELECT status, superseded_by FROM deployments WHERE id = $1",
                uuid.UUID(first.deployment_id),
            )
            assert old["status"] == "superseded"
            assert old["superseded_by"] == uuid.UUID(newer.deployment_id)
            old_job = await conn.fetchrow(
                "SELECT status FROM build_jobs WHERE deployment_id = $1",
                uuid.UUID(first.deployment_id),
            )
            assert old_job["status"] == "cancelled"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_enqueue_backpressure_is_fast_and_paid_plan_gets_a_position():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)
            admission = PostgresQueueAdmission(
                policy=QueueAdmissionPolicy(max_queue_depth=0)
            )
            store = PostgresGitStore(conn, admission=admission)

            started = time.perf_counter()
            with pytest.raises(QueueFull):
                await store.record_intent(
                    _intent(
                        project_id,
                        org_id,
                        sha="d" * 40,
                        delivery_id=f"{prefix}-full",
                    ),
                    received_at=NOW,
                )
            elapsed = time.perf_counter() - started
            # AC-4: the webhook's enqueue path must not block on capacity.
            assert elapsed < 0.2

            # The rejection is transactional: no queued deployment survives.
            queued = await conn.fetchval(
                "SELECT count(*) FROM deployments WHERE project_id = $1",
                project_id,
            )
            assert queued == 0

            await conn.execute(
                "UPDATE orgs SET plan_id = (SELECT id FROM plans WHERE code = 'pro') "
                "WHERE id = $1",
                org_id,
            )
            accepted = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="e" * 40,
                    delivery_id=f"{prefix}-paid",
                ),
                received_at=NOW,
            )
            assert accepted.created is True
            assert accepted.queue_position is not None
            assert accepted.queue_position >= 0
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_redis_admission_failure_rolls_back_and_leaves_no_orphan():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)

            class _FailingLimiter:
                async def allow(self, _org_id, *, now):
                    raise ConnectionError("redis is down")

            admission = PostgresQueueAdmission(limiter=_FailingLimiter())
            store = PostgresGitStore(conn, admission=admission)
            with pytest.raises(QueueUnavailable):
                await store.record_intent(
                    _intent(
                        project_id,
                        org_id,
                        sha="f" * 40,
                        delivery_id=f"{prefix}-redis-down",
                    ),
                    received_at=NOW,
                )

            assert await conn.fetchval(
                "SELECT count(*) FROM deployments WHERE project_id = $1",
                project_id,
            ) == 0
            reconciler = PostgresQueueReconciler(conn, clock=lambda: NOW)
            assert await reconciler.reconcile(
                now=NOW, org_id=str(org_id)
            ) == ()
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_preview_hostname_is_allocated_once_and_collisions_are_rejected():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)
            other_id = await _add_project(conn, org_id, uuid.uuid4().hex[:8])
            store = PostgresGitStore(conn)
            candidate = f"pr-1-demo-{uuid.uuid4().hex[:8]}.preview.example.test"
            first = await store.allocate_preview_hostname(
                str(project_id), "pr:1", candidate
            )
            second = await store.allocate_preview_hostname(
                str(project_id), "pr:1", "different.example.test"
            )
            assert first == second == candidate
            with pytest.raises(ValueError, match="collision"):
                await store.allocate_preview_hostname(
                    str(other_id), "pr:1", candidate
                )
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_delivery_claim_and_audit_event_share_one_row():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        delivery = f"{prefix}-claim"
        try:
            org_id, project_id = await _setup(conn)
            store = PostgresGitStore(conn)
            intent = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="c" * 40,
                    delivery_id=delivery,
                ),
                received_at=NOW,
            )
            assert await store.claim_delivery(delivery) is True
            assert await store.claim_delivery(delivery) is False
            await store.record_event(
                WebhookEventRecord(
                    delivery_id=delivery,
                    project_id=str(project_id),
                    event="push",
                    action=None,
                    outcome="accepted",
                    received_at=NOW,
                    git_ref="refs/heads/main",
                    git_sha="c" * 40,
                    deployment_id=intent.deployment_id,
                )
            )
            row = await conn.fetchrow(
                "SELECT event, ref, sha, action, deployment_id "
                "FROM webhook_deliveries WHERE delivery_id = $1",
                delivery,
            )
            assert row["event"] == "push"
            assert row["ref"] == "refs/heads/main"
            assert row["sha"] == "c" * 40
            assert row["action"] == "accepted"
            assert row["deployment_id"] == uuid.UUID(intent.deployment_id)

            await store.release_delivery(delivery)
            still_there = await conn.fetchval(
                "SELECT count(*) FROM webhook_deliveries WHERE delivery_id = $1",
                delivery,
            )
            assert still_there == 1
            assert await store.claim_delivery(delivery) is False

            # A claim that never got an audit event is safe to release so the
            # provider can retry the delivery.
            retry = f"{prefix}-release"
            assert await store.claim_delivery(retry) is True
            await store.release_delivery(retry)
            assert await store.claim_delivery(retry) is True
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())


def test_cancel_queued_cancels_jobs_and_leaves_running_work_alone():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        org_id = None
        prefix = f"test-delivery-{uuid.uuid4().hex}"
        try:
            org_id, project_id = await _setup(conn)
            store = PostgresGitStore(conn)
            queued = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="d" * 40,
                    delivery_id=f"{prefix}-cancel-a",
                    ref="refs/heads/one",
                ),
                received_at=NOW,
            )
            running = await store.record_intent(
                _intent(
                    project_id,
                    org_id,
                    sha="e" * 40,
                    delivery_id=f"{prefix}-cancel-b",
                    ref="refs/heads/two",
                ),
                received_at=NOW,
            )
            await conn.execute(
                "UPDATE deployments SET status = 'building', stage = 'build' "
                "WHERE id = $1",
                uuid.UUID(running.deployment_id),
            )
            await conn.execute(
                "UPDATE build_jobs SET status = 'running' "
                "WHERE deployment_id = $1",
                uuid.UUID(running.deployment_id),
            )

            cancelled = await store.cancel_queued(
                str(project_id), reason="project suspended", at=NOW
            )
            assert cancelled == 1
            statuses = dict(
                await conn.fetch(
                    "SELECT id::text, status FROM deployments "
                    "WHERE project_id = $1",
                    project_id,
                )
            )
            assert statuses[queued.deployment_id] == "cancelled"
            assert statuses[running.deployment_id] == "building"
            queued_job = await conn.fetchrow(
                "SELECT status FROM build_jobs WHERE deployment_id = $1",
                uuid.UUID(queued.deployment_id),
            )
            assert queued_job["status"] == "cancelled"
            running_job = await conn.fetchrow(
                "SELECT status FROM build_jobs WHERE deployment_id = $1",
                uuid.UUID(running.deployment_id),
            )
            assert running_job["status"] == "running"
        finally:
            if org_id is not None:
                await _cleanup(conn, org_id, prefix)
            await conn.close()

    asyncio.run(scenario())
