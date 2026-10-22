"""Live-Postgres acceptance tests for SPEC-12 (AC-1, AC-4..AC-8)."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from buildplane.registry.client import InMemoryContainerRegistry
from buildplane.registry.errors import ImageDigestMismatch, ImageQuotaExceeded
from buildplane.registry.models import (
    ImageArtifact,
    PublishRequest,
    digest_for,
    repository_for,
    revision_for,
)
from buildplane.registry.postgres import (
    PostgresImageGarbageCollector,
    PostgresImageStore,
)
from buildplane.registry.publisher import ContainerImagePublisher

DSN = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def _repo_id() -> int:
    return (uuid.uuid4().int % 9_000_000_000) + 1_000_000_000


async def _setup(conn, *, storage_gb: float = 1):
    tag = uuid.uuid4().hex[:10]
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    deployment_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    await conn.execute(
        """
        INSERT INTO plans (id, code, name, limits)
        VALUES ($1, $2, 'SPEC-12 test plan', $3::jsonb)
        """,
        plan_id,
        f"spec12-{tag}",
        '{"storage_gb": ' + str(storage_gb) + "}",
    )
    await conn.execute(
        """
        INSERT INTO orgs (id, name, slug, plan_id)
        VALUES ($1, $2, $3, $4)
        """,
        org_id,
        f"registry-{tag}",
        f"registry-{tag}",
        plan_id,
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
        f"project-{tag}",
        _repo_id(),
        f"ns-{tag}",
    )
    await conn.execute(
        """
        INSERT INTO deployments
            (id, project_id, org_id, git_sha, git_ref, environment,
             trigger_source, status, manifest, manifest_hash, build_image,
             build_env_keys, started_at, created_at)
        VALUES ($1, $2, $3, $4, 'refs/heads/main', 'production',
                'webhook', 'building', '{}'::jsonb, $5, $6, '{}'::text[],
                $7, $7)
        """,
        deployment_id,
        project_id,
        org_id,
        "a" * 40,
        "sha256:" + "b" * 64,
        "registry.example.test/builder@sha256:" + "c" * 64,
        NOW,
    )
    return {
        "tag": tag,
        "org_id": org_id,
        "project_id": project_id,
        "deployment_id": deployment_id,
        "plan_id": plan_id,
        "org_slug": f"registry-{tag}",
        "project_slug": f"project-{tag}",
    }


async def _cleanup(conn, ids):
    org_id = ids["org_id"]
    await conn.execute("DELETE FROM usage_daily WHERE org_id = $1", org_id)
    await conn.execute("DELETE FROM orgs WHERE id = $1", org_id)
    await conn.execute("DELETE FROM plans WHERE id = $1", ids["plan_id"])


def _request(ids, *, content=b"image-bytes", deployment_id=None):
    return PublishRequest(
        org_id=str(ids["org_id"]),
        project_id=str(ids["project_id"]),
        deployment_id=str(deployment_id or ids["deployment_id"]),
        registry="registry.platform.internal",
        repository=repository_for(ids["org_slug"], ids["project_slug"]),
        tag=revision_for("a" * 40, NOW),
        artifact=ImageArtifact(content),
        build_image_digest="sha256:" + "c" * 64,
        manifest_hash="sha256:" + "d" * 64,
    )


def test_publish_sets_digest_revision_and_storage_usage():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        ids = None
        try:
            ids = await _setup(conn)
            registry = InMemoryContainerRegistry()
            publisher = ContainerImagePublisher(
                registry=registry,
                store=PostgresImageStore(conn),
                clock=lambda: NOW,
            )
            request = _request(ids)
            image = await publisher.publish(request)

            row = await conn.fetchrow(
                """
                SELECT status, stage, revision, image_digest
                  FROM deployments WHERE id = $1
                """,
                ids["deployment_id"],
            )
            assert row["status"] == "building"
            assert row["stage"] == "pushed"
            assert row["revision"] == request.tag
            assert row["image_digest"] == request.digest

            stored = await conn.fetchrow(
                """
                SELECT tag, digest, size_bytes, build_image_digest, manifest_hash,
                       deployment_id
                  FROM images WHERE id = $1
                """,
                uuid.UUID(image.id),
            )
            assert stored["tag"] == row["revision"]
            assert stored["digest"] == row["image_digest"] == digest_for(
                request.artifact.content
            )
            assert stored["size_bytes"] == request.size_bytes
            assert str(stored["deployment_id"]) == str(ids["deployment_id"])

            usage = await conn.fetchval(
                "SELECT storage_bytes FROM usage_daily WHERE org_id = $1 AND day = $2",
                ids["org_id"],
                NOW.date(),
            )
            assert usage == request.size_bytes
        finally:
            if ids is not None:
                await _cleanup(conn, ids)
            await conn.close()

    asyncio.run(scenario())


def test_org_quota_rejection_reports_current_usage_and_limit():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        ids = None
        try:
            ids = await _setup(conn, storage_gb=0)
            publisher = ContainerImagePublisher(
                registry=InMemoryContainerRegistry(),
                store=PostgresImageStore(conn),
                clock=lambda: NOW,
            )
            with pytest.raises(ImageQuotaExceeded) as caught:
                await publisher.publish(_request(ids))
            error = caught.value
            assert error.code == "quota_exceeded"
            assert error.used_bytes == 0
            assert error.limit_bytes == 0
            assert "current usage=0 bytes" in str(error)
            assert "limit=0 bytes" in str(error)
            count = await conn.fetchval(
                "SELECT count(*) FROM images WHERE org_id = $1", ids["org_id"]
            )
            assert count == 0
        finally:
            if ids is not None:
                await _cleanup(conn, ids)
            await conn.close()

    asyncio.run(scenario())


class _WrongDigestRegistry:
    async def push(self, *, repository, tag, digest, content, credential=None):
        return "sha256:" + "f" * 64

    async def pull(self, *, repository, reference, credential=None):
        raise AssertionError("not used")

    async def delete(self, *, repository, reference):
        return True


def test_digest_mismatch_leaves_the_deployment_unverified_and_unready():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        ids = None
        try:
            ids = await _setup(conn)
            alerts = []
            publisher = ContainerImagePublisher(
                registry=_WrongDigestRegistry(),
                store=PostgresImageStore(conn),
                alert=alerts.append,
                clock=lambda: NOW,
            )
            with pytest.raises(ImageDigestMismatch) as caught:
                await publisher.publish(_request(ids))
            assert caught.value.retryable is False
            assert alerts and alerts[0].severity == "S2"

            row = await conn.fetchrow(
                """
                SELECT status, stage, revision, image_digest
                  FROM deployments WHERE id = $1
                """,
                ids["deployment_id"],
            )
            assert row["status"] == "building"
            assert row["revision"] is None
            assert row["image_digest"] is None
            count = await conn.fetchval(
                "SELECT count(*) FROM images WHERE org_id = $1", ids["org_id"]
            )
            assert count == 0
            with pytest.raises(asyncpg.exceptions.CheckViolationError):
                await conn.execute(
                    "UPDATE deployments SET status = 'ready' WHERE id = $1",
                    ids["deployment_id"],
                )
        finally:
            if ids is not None:
                await _cleanup(conn, ids)
            await conn.close()

    asyncio.run(scenario())


async def _insert_image(
    conn,
    ids,
    *,
    tag: str,
    digest_char: str,
    created_at: datetime,
    deployment_id=None,
):
    return await conn.fetchval(
        """
        INSERT INTO images
            (org_id, project_id, deployment_id, tag, digest, registry,
             size_bytes, build_image_digest, manifest_hash, created_at)
        VALUES ($1, $2, $3, $4, $5, 'registry.platform.internal', 10,
                $6, $7, $8)
        RETURNING id
        """,
        ids["org_id"],
        ids["project_id"],
        deployment_id,
        tag,
        "sha256:" + digest_char * 64,
        "sha256:" + "b" * 64,
        "sha256:" + "c" * 64,
        created_at,
    )


async def _insert_route(conn, ids, *, tag: str):
    await conn.execute(
        """
        INSERT INTO routes (org_id, project_id, hostname, path_prefix, revision)
        VALUES ($1, $2, $3, '/', $4)
        """,
        ids["org_id"],
        ids["project_id"],
        f"{uuid.uuid4().hex[:12]}.test",
        tag,
    )


def test_gc_respects_routes_and_unmarks_newly_referenced_images():
    async def scenario():
        conn = await asyncpg.connect(DSN)
        ids = None
        try:
            ids = await _setup(conn)
            old = NOW - timedelta(days=45)
            live_tag = revision_for("1" * 40, NOW)
            newly_tag = revision_for("2" * 40, NOW)
            delete_tag = revision_for("3" * 40, NOW)
            await _insert_image(
                conn, ids, tag=live_tag, digest_char="1", created_at=old
            )
            newly_id = await _insert_image(
                conn, ids, tag=newly_tag, digest_char="2", created_at=old
            )
            delete_id = await _insert_image(
                conn, ids, tag=delete_tag, digest_char="3", created_at=old
            )
            await _insert_route(conn, ids, tag=live_tag)

            gc = PostgresImageGarbageCollector(
                conn, InMemoryContainerRegistry(), clock=lambda: NOW
            )
            report = await gc.mark(now=NOW - timedelta(days=10))
            assert report.marked == 2
            assert report.unmarked == 0

            rows = await conn.fetch(
                "SELECT id, tag, marked_for_deletion_at FROM images WHERE org_id = $1",
                ids["org_id"],
            )
            marked = {row["tag"]: row["marked_for_deletion_at"] for row in rows}
            assert marked[live_tag] is None
            assert marked[newly_tag] == NOW - timedelta(days=10)
            assert marked[delete_tag] == NOW - timedelta(days=10)

            # AC-5: a reference created after the mark run makes the image live
            # again, and the delete pass must unmark rather than delete it.
            await _insert_route(conn, ids, tag=newly_tag)
            report = await gc.delete_marked(now=NOW)
            assert report.unmarked == 1
            assert report.deleted == 1

            remaining = await conn.fetch(
                "SELECT id, tag, marked_for_deletion_at FROM images WHERE org_id = $1",
                ids["org_id"],
            )
            remaining_by_tag = {row["tag"]: row for row in remaining}
            assert live_tag in remaining_by_tag
            assert newly_tag in remaining_by_tag
            assert remaining_by_tag[newly_tag]["marked_for_deletion_at"] is None
            assert delete_tag not in remaining_by_tag
            assert uuid.UUID(str(delete_id)) not in {
                row["id"] for row in remaining
            }
            assert uuid.UUID(str(newly_id)) in {row["id"] for row in remaining}
        finally:
            if ids is not None:
                await _cleanup(conn, ids)
            await conn.close()

    asyncio.run(scenario())
