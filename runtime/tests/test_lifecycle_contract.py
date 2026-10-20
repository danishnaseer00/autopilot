"""SPEC-13 acceptance criteria against a live Postgres database.

Requires the compose stack from ``paas/docker-compose.dev.yml`` and:
  DATABASE_URL      migrator DSN (schema + 202610201006 applied)
  APP_DATABASE_URL  autopilot_app DSN (RLS subject)

Skips cleanly when the DSNs are absent. Each test provisions its own org,
project and route rows as the migrator, then deletes them. Audit is the
in-memory sink: SPEC-18's contract suite already proves the real audit path.

Cluster-only criteria are intentionally not faked here:
  AC-7  (zero 502s)                     -> coordinator, cluster
  AC-10 (in-flight requests complete)   -> coordinator, cluster
  AC-11 (long-lived connections)        -> coordinator, cluster + SPEC-17
The drain bound those tests must honour is unit-tested in
``test_lifecycle_state.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import uuid

import asyncpg
import pytest

from runtime.lifecycle import (
    DeploymentLifecycle,
    DrainPolicy,
    EnqueueFailed,
    InvalidDeployment,
    NoRoute,
    ProjectNotFound,
    DeploymentNotFound,
)
from runtime.lifecycle.errors import AuditUnavailable
from runtime.secrets.audit import InMemoryAuditSink

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") and os.environ.get("APP_DATABASE_URL")),
    reason="DATABASE_URL / APP_DATABASE_URL not configured",
)

MIGRATOR_DSN = os.environ.get("DATABASE_URL", "")
APP_DSN = os.environ.get("APP_DATABASE_URL", "")

MANIFEST = {
    "version": 1,
    "name": "shop",
    "services": [
        {
            "name": "web",
            "source": ".",
            "type": "node",
            "runtime": {"port": 3000},
            "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}},
        }
    ],
}
BUILD_IMAGE = "ghcr.io/autopilot/build-node@sha256:" + "a" * 64


def _revision(sha: str) -> str:
    return "sha256:" + hashlib.sha256(sha.encode("utf-8")).hexdigest()


def _digest(sha: str) -> str:
    return "sha256:" + hashlib.sha256(("image:" + sha).encode("utf-8")).hexdigest()


# ===== fixtures =============================================================


@pytest.fixture
def tenants():
    state = asyncio.run(_provision())
    try:
        yield state
    finally:
        asyncio.run(_cleanup(state))


async def _provision() -> dict:
    tag = uuid.uuid4().hex[:12]
    conn = await asyncpg.connect(MIGRATOR_DSN)
    try:
        org = await conn.fetchval(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING id",
            f"Lifecycle {tag}",
            f"lifecycle-{tag}",
        )
        project = await conn.fetchval(
            "INSERT INTO projects "
            "  (org_id, name, slug, provider, provider_repo_id, k8s_namespace) "
            "VALUES ($1, $2, $3, 'github', $4, $5) RETURNING id",
            org,
            "Storefront",
            f"storefront-{tag}",
            int(tag, 16) % 2_000_000_000,
            f"autopilot-{tag}",
        )
        other_org = await conn.fetchval(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING id",
            f"Other {tag}",
            f"other-{tag}",
        )
        other_project = await conn.fetchval(
            "INSERT INTO projects "
            "  (org_id, name, slug, provider, provider_repo_id, k8s_namespace) "
            "VALUES ($1, $2, $3, 'github', $4, $5) RETURNING id",
            other_org,
            "Other",
            f"other-project-{tag}",
            int(tag, 16) % 2_000_000_000 + 1,
            f"autopilot-other-{tag}",
        )
        prod_host = f"app-{tag}.example.test"
        preview_host = f"preview-{tag}.example.test"
        await conn.execute(
            "INSERT INTO routes (org_id, project_id, hostname, path_prefix) "
            "VALUES ($1, $2, $3, '/'), ($1, $2, $4, '/')",
            org,
            project,
            prod_host,
            preview_host,
        )
        # A SPEC-15 apex-style redirect shares the hostname but is not a
        # revision of the app; promotion must never point it at a deployment.
        await conn.execute(
            "INSERT INTO routes "
            "  (org_id, project_id, hostname, path_prefix, kind, target_path, status_code) "
            "VALUES ($1, $2, $3, '/old', 'redirect', '/', 301)",
            org,
            project,
            prod_host,
        )
        return {
            "tag": tag,
            "org_id": org,
            "project_id": project,
            "other_org_id": other_org,
            "other_project_id": other_project,
            "prod_host": prod_host,
            "preview_host": preview_host,
            "hostnames": [prod_host, preview_host],
        }
    finally:
        await conn.close()


async def _cleanup(state: dict) -> None:
    conn = await asyncpg.connect(MIGRATOR_DSN)
    try:
        await conn.execute(
            "DELETE FROM route_history WHERE hostname = ANY($1::text[])",
            state["hostnames"],
        )
        # The self-reference would otherwise make the cascade order matter.
        await conn.execute(
            "UPDATE deployments SET superseded_by = NULL "
            " WHERE org_id = ANY($1::uuid[])",
            [state["org_id"], state["other_org_id"]],
        )
        await conn.execute(
            "DELETE FROM orgs WHERE id = ANY($1::uuid[])",
            [state["org_id"], state["other_org_id"]],
        )
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def _app_tx(org_id: uuid.UUID):
    conn = await asyncpg.connect(APP_DSN)
    tx = conn.transaction()
    await tx.start()
    await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
    try:
        yield conn
    except BaseException:
        await tx.rollback()
        raise
    else:
        await tx.commit()
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def _db():
    conn = await asyncpg.connect(MIGRATOR_DSN)
    try:
        yield conn
    finally:
        await conn.close()


def _service(
    audit: InMemoryAuditSink | None = None,
    queue=None,
) -> DeploymentLifecycle:
    return DeploymentLifecycle(
        audit=audit or InMemoryAuditSink(),
        queue=queue,
        drain_policy=DrainPolicy(),
    )


async def _trigger(
    service: DeploymentLifecycle,
    tenants: dict,
    sha: str,
    *,
    ref: str = "refs/heads/main",
    environment: str = "production",
    org_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
):
    org = org_id or tenants["org_id"]
    project = project_id or tenants["project_id"]
    async with _app_tx(org) as conn:
        return await service.trigger(
            conn,
            org_id=org,
            project_id=project,
            git_sha=sha,
            git_ref=ref,
            environment=environment,
            manifest=MANIFEST,
            build_image=BUILD_IMAGE,
            actor_id="autopilot",
            actor_type="system",
        )


async def _claim(service: DeploymentLifecycle, org: uuid.UUID, deployment_id: uuid.UUID):
    async with _app_tx(org) as conn:
        return await service.claim(conn, org_id=org, deployment_id=deployment_id)


async def _ready(
    service: DeploymentLifecycle, org: uuid.UUID, deployment_id: uuid.UUID, sha: str
):
    await _claim(service, org, deployment_id)
    async with _app_tx(org) as conn:
        return await service.mark_ready(
            conn,
            org_id=org,
            deployment_id=deployment_id,
            revision=_revision(sha),
            image_digest=_digest(sha),
        )


async def _promote(
    service: DeploymentLifecycle,
    org: uuid.UUID,
    deployment_id: uuid.UUID,
    *,
    hostname: str,
    actor_id: str = "autopilot",
    actor_type: str = "system",
):
    async with _app_tx(org) as conn:
        return await service.promote(
            conn,
            org_id=org,
            deployment_id=deployment_id,
            actor_id=actor_id,
            actor_type=actor_type,
            hostnames=[hostname],
        )


async def _rollback(
    service: DeploymentLifecycle,
    org: uuid.UUID,
    deployment_id: uuid.UUID,
    *,
    hostname: str | None = None,
    actor_id: str = "alice",
    actor_type: str = "user",
):
    async with _app_tx(org) as conn:
        return await service.rollback(
            conn,
            org_id=org,
            deployment_id=deployment_id,
            actor_id=actor_id,
            actor_type=actor_type,
            hostnames=[hostname] if hostname else None,
        )


async def _row(deployment_id: uuid.UUID):
    async with _db() as conn:
        return await conn.fetchrow(
            "SELECT status, revision, superseded_by, finished_at "
            "  FROM deployments WHERE id = $1",
            deployment_id,
        )


async def _route(hostname: str):
    async with _db() as conn:
        return await conn.fetchrow(
            "SELECT deployment_id, revision, updated_at "
            "  FROM routes WHERE hostname = $1 AND path_prefix = '/'",
            hostname,
        )


async def _count(sql: str, *args) -> int:
    async with _db() as conn:
        return int(await conn.fetchval(sql, *args))


# ===== AC-1 / AC-2: state invariants are not app-only ======================


def test_ac1_ready_requires_a_verified_revision(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        triggered = await _trigger(service, tenants, "1" * 40)
        await _claim(service, org, triggered.deployment.id)

        with pytest.raises(InvalidDeployment):
            async with _app_tx(org) as conn:
                await service.mark_ready(
                    conn,
                    org_id=org,
                    deployment_id=triggered.deployment.id,
                    revision="",
                    image_digest=_digest("1" * 40),
                )

        ready = await _ready(service, org, triggered.deployment.id, "1" * 40)
        assert ready.status == "ready"
        assert ready.revision == _revision("1" * 40)

        # The database CHECK is the backstop, not just the service.
        queued = await _trigger(service, tenants, "2" * 40)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with _app_tx(org) as conn:
                await conn.execute(
                    "UPDATE deployments SET status = 'ready' WHERE id = $1",
                    queued.deployment.id,
                )

    asyncio.run(scenario())


def test_ac2_failed_always_has_finished_at(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        triggered = await _trigger(service, tenants, "3" * 40)
        await _claim(service, org, triggered.deployment.id)
        async with _app_tx(org) as conn:
            failed = await service.mark_failed(
                conn,
                org_id=org,
                deployment_id=triggered.deployment.id,
                stage="build",
                error_message="compiler exited",
                exit_code=2,
            )
        assert failed.status == "failed"
        assert failed.finished_at is not None
        assert failed.exit_code == 2

        queued = await _trigger(service, tenants, "4" * 40)
        with pytest.raises(asyncpg.exceptions.CheckViolationError):
            async with _app_tx(org) as conn:
                await conn.execute(
                    "UPDATE deployments SET status = 'failed' WHERE id = $1",
                    queued.deployment.id,
                )

    asyncio.run(scenario())


# ===== AC-3: duplicate delivery ============================================


def test_ac3_same_push_five_times_builds_once(tenants) -> None:
    async def scenario() -> None:
        sink = InMemoryAuditSink()
        service = _service(audit=sink)
        sha = "5" * 40
        results = [await _trigger(service, tenants, sha) for _ in range(5)]

        assert results[0].created is True
        assert all(result.created is False for result in results[1:])
        assert len({result.deployment.id for result in results}) == 1
        deployment_id = results[0].deployment.id

        assert (
            await _count(
                "SELECT count(*) FROM deployments "
                " WHERE project_id = $1 AND git_sha = $2 AND git_ref = $3",
                tenants["project_id"],
                sha,
                "refs/heads/main",
            )
            == 1
        )
        assert (
            await _count(
                "SELECT count(*) FROM build_jobs WHERE deployment_id = $1",
                deployment_id,
            )
            == 1
        )
        assert sink.actions().count("deployment.trigger") == 1

    asyncio.run(scenario())


# ===== AC-4 / R-6: the newest SHA wins even if it finishes first ============


def test_ac4_slowest_build_never_goes_live(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        shas = ["6" * 40, "7" * 40, "8" * 40]
        deployments = {}
        for sha in shas:
            triggered = await _trigger(service, tenants, sha)
            await _claim(service, org, triggered.deployment.id)
            deployments[sha] = triggered.deployment

        # The middle push finishes first, then the newest, then the oldest.
        await _ready(service, org, deployments[shas[1]].id, shas[1])
        await _ready(service, org, deployments[shas[2]].id, shas[2])
        second = await _promote(
            service, org, deployments[shas[1]].id, hostname=tenants["prod_host"]
        )
        third = await _promote(
            service, org, deployments[shas[2]].id, hostname=tenants["prod_host"]
        )
        assert second.deployment.status == "live"
        assert third.deployment.status == "live"
        assert third.routes_updated == 1

        # The oldest finishes last; it must supersede itself, not cut over.
        old = await _ready(service, org, deployments[shas[0]].id, shas[0])
        assert old.status == "superseded"
        old_promote = await _promote(
            service, org, deployments[shas[0]].id, hostname=tenants["prod_host"]
        )
        assert old_promote.deployment.status == "superseded"
        assert old_promote.routes_updated == 0

        live = await _count(
            "SELECT count(*) FROM deployments "
            " WHERE project_id = $1 AND git_ref = $2 AND status = 'live'",
            tenants["project_id"],
            "refs/heads/main",
        )
        assert live == 1
        rows = 0
        async with _db() as conn:
            rows = await conn.fetch(
                "SELECT git_sha, status FROM deployments "
                " WHERE project_id = $1 AND git_ref = $2",
                tenants["project_id"],
                "refs/heads/main",
            )
        by_sha = {row["git_sha"]: row["status"] for row in rows}
        assert by_sha[shas[2]] == "live"
        assert by_sha[shas[1]] == "superseded"
        assert by_sha[shas[0]] == "superseded"

        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] == deployments[shas[2]].id
        assert route["revision"] == _revision(shas[2])

    asyncio.run(scenario())


# ===== AC-5 / R-7 / R-8: a preview never cancels production ================


def test_ac5_preview_push_does_not_cancel_production(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        p1 = await _trigger(service, tenants, "9" * 40)
        pr1 = await _trigger(
            service,
            tenants,
            "a" * 40,
            ref="refs/pull/7/head",
            environment="preview",
        )
        pr2 = await _trigger(
            service,
            tenants,
            "b" * 40,
            ref="refs/pull/7/head",
            environment="preview",
        )
        # The preview push cancels only the older preview build.
        assert (await _row(p1.deployment.id))["status"] == "queued"
        assert (await _row(pr1.deployment.id))["status"] == "cancelled"
        assert (await _row(pr2.deployment.id))["status"] == "queued"

        p2 = await _trigger(service, tenants, "c" * 40)
        # The production push cancels only the older production build.
        assert (await _row(p1.deployment.id))["status"] == "cancelled"
        assert (await _row(pr2.deployment.id))["status"] == "queued"
        assert (await _row(p2.deployment.id))["status"] == "queued"

        async with _db() as conn:
            jobs = await conn.fetch(
                "SELECT deployment_id, status::text AS status FROM build_jobs "
                " WHERE deployment_id = ANY($1::uuid[])",
                [
                    p1.deployment.id,
                    pr1.deployment.id,
                    pr2.deployment.id,
                    p2.deployment.id,
                ],
            )
        by_id = {row["deployment_id"]: row["status"] for row in jobs}
        assert by_id[p1.deployment.id] == "cancelled"
        assert by_id[pr1.deployment.id] == "cancelled"
        assert by_id[pr2.deployment.id] == "pending"
        assert by_id[p2.deployment.id] == "pending"

    asyncio.run(scenario())


# ===== AC-6: a failed rollout leaves the route alone =======================


def test_ac6_failed_rollout_keeps_the_previous_revision_serving(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        old = await _trigger(service, tenants, "d" * 40)
        await _ready(service, org, old.deployment.id, "d" * 40)
        await _promote(service, org, old.deployment.id, hostname=tenants["prod_host"])

        new = await _trigger(service, tenants, "e" * 40)
        ready = await _ready(service, org, new.deployment.id, "e" * 40)
        assert ready.status == "ready"

        async with _app_tx(org) as conn:
            failed = await service.mark_failed(
                conn,
                org_id=org,
                deployment_id=new.deployment.id,
                stage="rollout",
                error_message="readiness probe timed out",
            )
        assert failed.status == "failed"
        assert failed.finished_at is not None

        assert (await _row(old.deployment.id))["status"] == "live"
        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] == old.deployment.id
        assert route["revision"] == _revision("d" * 40)
        assert (
            await _count(
                "SELECT count(*) FROM route_history WHERE deployment_id = $1",
                new.deployment.id,
            )
            == 0
        )

    asyncio.run(scenario())


# ===== AC-8 / AC-9 / AC-13: rollback is one transactional cutover ==========


def test_ac8_ac9_ac13_rollback_is_a_transactional_cutover(tenants) -> None:
    async def scenario() -> None:
        sink = InMemoryAuditSink()
        service = _service(audit=sink)
        org = tenants["org_id"]
        first = await _trigger(service, tenants, "f" * 40)
        await _ready(service, org, first.deployment.id, "f" * 40)
        first_promote = await _promote(
            service, org, first.deployment.id, hostname=tenants["prod_host"]
        )
        assert first_promote.routes_updated == 1

        second = await _trigger(service, tenants, "1" * 40)
        await _ready(service, org, second.deployment.id, "1" * 40)
        second_promote = await _promote(
            service, org, second.deployment.id, hostname=tenants["prod_host"]
        )
        assert second_promote.routes_updated == 1
        assert (await _row(first.deployment.id))["status"] == "superseded"

        jobs_before = await _count(
            "SELECT count(*) FROM build_jobs WHERE org_id = $1", org
        )
        rollback = await _rollback(service, org, first.deployment.id)
        assert rollback.deployment.status == "live"
        assert rollback.routes_updated == 1
        assert rollback.superseded == (second.deployment.id,)
        first_row = await _row(first.deployment.id)
        assert first_row["status"] == "live"
        assert first_row["finished_at"] is None
        assert first_row["superseded_by"] is None
        second_row = await _row(second.deployment.id)
        assert second_row["status"] == "superseded"
        assert second_row["finished_at"] is not None
        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] == first.deployment.id
        assert route["revision"] == _revision("f" * 40)
        # AC-8: no rebuild happened during the cutover.
        assert (
            await _count("SELECT count(*) FROM build_jobs WHERE org_id = $1", org)
            == jobs_before
        )

        async with _db() as conn:
            history = await conn.fetch(
                "SELECT from_revision, to_revision, actor, created_at "
                "  FROM route_history WHERE hostname = $1 ORDER BY id",
                tenants["prod_host"],
            )
        assert len(history) == 3
        assert [(row["from_revision"], row["to_revision"], row["actor"]) for row in history] == [
            (None, _revision("f" * 40), "system:autopilot"),
            (_revision("f" * 40), _revision("1" * 40), "system:autopilot"),
            (_revision("1" * 40), _revision("f" * 40), "user:alice"),
        ]
        assert all(row["created_at"] is not None for row in history)
        assert sink.actions().count("deployment.rollback") == 1

        # AC-8's atomicity: abort after the cutover and nothing persists.
        with pytest.raises(RuntimeError):
            async with _app_tx(org) as conn:
                await service.rollback(
                    conn,
                    org_id=org,
                    deployment_id=second.deployment.id,
                    actor_id="alice",
                    actor_type="user",
                )
                raise RuntimeError("abort after cutover")
        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] == first.deployment.id
        assert (await _row(first.deployment.id))["status"] == "live"
        assert (await _row(second.deployment.id))["status"] == "superseded"

    asyncio.run(scenario())


# ===== AC-12: the ref is part of the identity ==============================


def test_ac12_same_sha_on_two_refs_is_two_deployments(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        sha = "2" * 40
        main = await _trigger(service, tenants, sha)
        preview = await _trigger(
            service,
            tenants,
            sha,
            ref="refs/pull/9/head",
            environment="preview",
        )
        assert main.deployment.id != preview.deployment.id

        # Once the first deployment finished, the same SHA is a redeploy.
        await _ready(service, tenants["org_id"], main.deployment.id, sha)
        again = await _trigger(service, tenants, sha)
        assert again.created is True
        assert again.deployment.id != main.deployment.id
        assert (
            await _count(
                "SELECT count(*) FROM deployments WHERE project_id = $1 AND git_sha = $2",
                tenants["project_id"],
                sha,
            )
            == 3
        )

    asyncio.run(scenario())


# ===== duplicate delivery job cancellation =================================


def test_a_newer_push_cancels_the_older_queued_job(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        old = await _trigger(service, tenants, "3" * 40)
        new = await _trigger(service, tenants, "4" * 40)
        old_row = await _row(old.deployment.id)
        assert old_row["status"] == "cancelled"
        assert old_row["finished_at"] is not None
        assert old_row["superseded_by"] == new.deployment.id
        async with _db() as conn:
            old_job = await conn.fetchval(
                "SELECT status::text FROM build_jobs WHERE deployment_id = $1",
                old.deployment.id,
            )
            new_job = await conn.fetchval(
                "SELECT status::text FROM build_jobs WHERE deployment_id = $1",
                new.deployment.id,
            )
        assert old_job == "cancelled"
        assert new_job == "pending"

    asyncio.run(scenario())


# ===== tenant isolation ====================================================


def test_cross_tenant_trigger_and_read_are_rejected(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        with pytest.raises(ProjectNotFound):
            await _trigger(
                service,
                tenants,
                "5" * 40,
                project_id=tenants["other_project_id"],
            )

        other = await _trigger(
            service,
            tenants,
            "6" * 40,
            org_id=tenants["other_org_id"],
            project_id=tenants["other_project_id"],
        )
        with pytest.raises(DeploymentNotFound):
            async with _app_tx(tenants["org_id"]) as conn:
                await service.get(
                    conn,
                    org_id=tenants["org_id"],
                    deployment_id=other.deployment.id,
                )
        with pytest.raises(DeploymentNotFound):
            await _promote(
                service,
                tenants["org_id"],
                other.deployment.id,
                hostname=tenants["prod_host"],
            )

    asyncio.run(scenario())


# ===== fail-closed boundaries ==============================================


def test_enqueue_failure_rolls_the_deployment_back(tenants) -> None:
    class FailingQueue:
        async def enqueue(
            self,
            conn,
            *,
            deployment_id,
            org_id,
            project_id,
            priority=0,
        ):
            raise RuntimeError("queue unavailable")

    async def scenario() -> None:
        service = _service(queue=FailingQueue())
        with pytest.raises(EnqueueFailed):
            await _trigger(service, tenants, "7" * 40)
        assert (
            await _count(
                "SELECT count(*) FROM deployments "
                " WHERE project_id = $1 AND git_sha = $2",
                tenants["project_id"],
                "7" * 40,
            )
            == 0
        )

    asyncio.run(scenario())


def test_audit_failure_refuses_trigger_and_promotion(tenants) -> None:
    async def scenario() -> None:
        bad_sink = InMemoryAuditSink(fail=True)
        bad = _service(audit=bad_sink)
        org = tenants["org_id"]
        with pytest.raises(AuditUnavailable):
            await _trigger(bad, tenants, "8" * 40)
        assert (
            await _count(
                "SELECT count(*) FROM deployments "
                " WHERE project_id = $1 AND git_sha = $2",
                tenants["project_id"],
                "8" * 40,
            )
            == 0
        )

        service = _service()
        triggered = await _trigger(service, tenants, "9" * 40)
        await _ready(service, org, triggered.deployment.id, "9" * 40)
        with pytest.raises(AuditUnavailable):
            async with _app_tx(org) as conn:
                await bad.promote(
                    conn,
                    org_id=org,
                    deployment_id=triggered.deployment.id,
                    actor_id="autopilot",
                    actor_type="system",
                    hostnames=[tenants["prod_host"]],
                )
        assert (await _row(triggered.deployment.id))["status"] == "ready"
        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] is None

    asyncio.run(scenario())


# ===== idempotent cutover and first-deploy routing =========================


def test_promotion_is_idempotent(tenants) -> None:
    async def scenario() -> None:
        sink = InMemoryAuditSink()
        service = _service(audit=sink)
        org = tenants["org_id"]
        triggered = await _trigger(service, tenants, "a" * 40)
        await _ready(service, org, triggered.deployment.id, "a" * 40)
        first = await _promote(
            service, org, triggered.deployment.id, hostname=tenants["prod_host"]
        )
        second = await _promote(
            service, org, triggered.deployment.id, hostname=tenants["prod_host"]
        )
        assert first.changed is True
        assert first.routes_updated == 1
        assert second.changed is False
        assert second.routes_updated == 0
        assert second.deployment.status == "live"
        assert (
            await _count(
                "SELECT count(*) FROM route_history WHERE hostname = $1",
                tenants["prod_host"],
            )
            == 1
        )
        assert sink.actions().count("deployment.promote") == 1

        # A worker retry after the promotion must not fail: the revision is
        # already recorded, so mark_ready is a no-op on a live deployment.
        async with _app_tx(org) as conn:
            retried = await service.mark_ready(
                conn,
                org_id=org,
                deployment_id=triggered.deployment.id,
                revision=_revision("a" * 40),
                image_digest=_digest("a" * 40),
            )
        assert retried.status == "live"

    asyncio.run(scenario())


def test_first_deployment_needs_explicit_hostnames(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        triggered = await _trigger(service, tenants, "b" * 40)
        await _ready(service, org, triggered.deployment.id, "b" * 40)
        with pytest.raises(NoRoute):
            async with _app_tx(org) as conn:
                await service.promote(
                    conn,
                    org_id=org,
                    deployment_id=triggered.deployment.id,
                    actor_id="autopilot",
                    actor_type="system",
                )
        assert (await _row(triggered.deployment.id))["status"] == "ready"
        route = await _route(tenants["prod_host"])
        assert route["deployment_id"] is None

    asyncio.run(scenario())


def test_promotion_ignores_redirect_rows_on_the_same_hostname(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        org = tenants["org_id"]
        triggered = await _trigger(service, tenants, "c" * 40)
        await _ready(service, org, triggered.deployment.id, "c" * 40)
        promoted = await _promote(
            service, org, triggered.deployment.id, hostname=tenants["prod_host"]
        )
        assert promoted.routes_updated == 1
        async with _db() as conn:
            redirect = await conn.fetchrow(
                "SELECT kind::text AS kind, deployment_id, revision "
                "  FROM routes WHERE hostname = $1 AND path_prefix = '/old'",
                tenants["prod_host"],
            )
        assert redirect["kind"] == "redirect"
        assert redirect["deployment_id"] is None
        assert redirect["revision"] is None

    asyncio.run(scenario())
