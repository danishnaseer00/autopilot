"""Postgres adapters for SPEC-12 publication and two-phase GC.

The baseline schema already has ``images``, ``usage_daily`` and the deployment
pointer columns. No new table or column is required: the registry host lives in
``images.registry``, the repository is derivable from the org/project slugs,
and the mark timestamp is ``images.marked_for_deletion_at``.

All mutating methods run in a transaction. The org row is locked while quota is
checked so two workers cannot both spend the last bytes of a plan.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg

from buildplane.registry.client import ContainerRegistry
from buildplane.registry.errors import (
    ImageIdentityConflict,
    ImageQuotaExceeded,
    ImageTooLarge,
    RegistryError,
)
from buildplane.registry.models import (
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_ORG_STORAGE_BYTES,
    ImagePublication,
    PublishedImage,
    REGISTRY_NAMESPACE,
    StorageUsage,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class GcReport:
    marked: int = 0
    unmarked: int = 0
    deleted: int = 0
    retained: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _storage_limit_bytes(limits: Any) -> int:
    if isinstance(limits, str):
        try:
            limits = json.loads(limits)
        except json.JSONDecodeError:
            limits = {}
    if not isinstance(limits, dict):
        return DEFAULT_ORG_STORAGE_BYTES
    value = limits.get("storage_gb")
    if value is None:
        value = limits.get("storageBytes")
        if value is not None:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return DEFAULT_ORG_STORAGE_BYTES
    try:
        return max(0, int(float(value) * 1024 * 1024 * 1024))
    except (TypeError, ValueError):
        return DEFAULT_ORG_STORAGE_BYTES


def _image_from_row(row: asyncpg.Record) -> PublishedImage:
    return PublishedImage(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        project_id=str(row["project_id"]),
        deployment_id=(
            str(row["deployment_id"]) if row["deployment_id"] is not None else None
        ),
        tag=row["tag"],
        digest=row["digest"],
        registry=row["registry"],
        size_bytes=int(row["size_bytes"]),
        build_image_digest=row["build_image_digest"],
        manifest_hash=row["manifest_hash"],
        marked_for_deletion_at=row["marked_for_deletion_at"],
    )


class PostgresImageStore:
    """Quota-aware image record store used by ``ContainerImagePublisher``."""

    def __init__(
        self,
        connection: asyncpg.Connection,
        *,
        clock=_utcnow,
        image_limit_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        self.connection = connection
        self._clock = clock
        self.image_limit_bytes = image_limit_bytes

    async def usage(self, org_id: str) -> StorageUsage:
        row = await self.connection.fetchrow(
            """
            SELECT COALESCE(SUM(i.size_bytes), 0)::bigint AS used_bytes,
                   p.limits
              FROM orgs o
              LEFT JOIN plans p ON p.id = o.plan_id
              LEFT JOIN images i ON i.org_id = o.id
             WHERE o.id = $1
             GROUP BY p.limits
            """,
            org_id,
        )
        if row is None:
            raise RegistryError("organisation does not exist")
        return StorageUsage(
            used_bytes=int(row["used_bytes"]),
            limit_bytes=_storage_limit_bytes(row["limits"]),
            image_limit_bytes=self.image_limit_bytes,
        )

    async def record(
        self, publication: ImagePublication, *, at: datetime
    ) -> PublishedImage:
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        if publication.size_bytes > self.image_limit_bytes:
            raise ImageTooLarge(
                size_bytes=publication.size_bytes,
                limit_bytes=self.image_limit_bytes,
            )
        async with self.connection.transaction():
            org = await self.connection.fetchrow(
                "SELECT id FROM orgs WHERE id = $1 FOR UPDATE",
                publication.org_id,
            )
            if org is None:
                raise RegistryError("organisation does not exist")

            identity = await self.connection.fetchrow(
                """
                SELECT o.slug AS org_slug, p.slug AS project_slug
                  FROM projects p
                  JOIN orgs o ON o.id = p.org_id
                 WHERE p.id = $1 AND p.org_id = $2
                """,
                publication.project_id,
                publication.org_id,
            )
            if identity is None:
                raise RegistryError("project does not exist for this organisation")
            expected = (
                f"{REGISTRY_NAMESPACE}/{identity['org_slug']}/"
                f"{identity['project_slug']}"
            )
            if publication.repository != expected:
                raise RegistryError(
                    "repository does not match the organisation/project slug"
                )

            existing = await self.connection.fetchrow(
                """
                SELECT id, org_id, project_id, deployment_id, tag, digest,
                       registry, size_bytes, build_image_digest, manifest_hash,
                       marked_for_deletion_at
                  FROM images
                 WHERE digest = $1
                 FOR UPDATE
                """,
                publication.digest,
            )
            if existing is not None:
                same_revision = (
                    str(existing["org_id"]) == publication.org_id
                    and str(existing["project_id"]) == publication.project_id
                    and existing["deployment_id"] is not None
                    and str(existing["deployment_id"]) == publication.deployment_id
                    and existing["tag"] == publication.tag
                )
                if not same_revision:
                    raise ImageIdentityConflict(publication.digest)
                await self.connection.execute(
                    "UPDATE images SET marked_for_deletion_at = NULL WHERE id = $1",
                    existing["id"],
                )
                await self._set_deployment_pointer(publication)
                await self._write_usage(publication.org_id, at.date())
                updated = await self.connection.fetchrow(
                    """
                    SELECT id, org_id, project_id, deployment_id, tag, digest,
                           registry, size_bytes, build_image_digest, manifest_hash,
                           marked_for_deletion_at
                      FROM images WHERE id = $1
                    """,
                    existing["id"],
                )
                assert updated is not None
                return _image_from_row(updated)

            limit = await self._org_limit(publication.org_id)
            used = await self.connection.fetchval(
                "SELECT COALESCE(SUM(size_bytes), 0)::bigint FROM images WHERE org_id = $1",
                publication.org_id,
            )
            used = int(used or 0)
            if used + publication.size_bytes > limit:
                raise ImageQuotaExceeded(
                    org_id=publication.org_id,
                    used_bytes=used,
                    limit_bytes=limit,
                    requested_bytes=publication.size_bytes,
                )

            await self._set_deployment_pointer(publication)
            row = await self.connection.fetchrow(
                """
                INSERT INTO images
                    (org_id, project_id, deployment_id, tag, digest, registry,
                     size_bytes, build_image_digest, manifest_hash, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING id, org_id, project_id, deployment_id, tag, digest,
                          registry, size_bytes, build_image_digest, manifest_hash,
                          marked_for_deletion_at
                """,
                publication.org_id,
                publication.project_id,
                publication.deployment_id,
                publication.tag,
                publication.digest,
                publication.registry,
                publication.size_bytes,
                publication.build_image_digest,
                publication.manifest_hash,
                at,
            )
            assert row is not None
            await self._write_usage(publication.org_id, at.date())
            return _image_from_row(row)

    async def _set_deployment_pointer(self, publication: ImagePublication) -> None:
        row = await self.connection.fetchrow(
            """
            SELECT project_id, status, revision, image_digest
              FROM deployments
             WHERE id = $1 AND org_id = $2
             FOR UPDATE
            """,
            publication.deployment_id,
            publication.org_id,
        )
        if row is None:
            raise RegistryError("deployment does not exist for this organisation")
        if str(row["project_id"]) != publication.project_id:
            raise RegistryError("deployment does not belong to this project")
        if row["status"] in {"failed", "cancelled", "superseded"}:
            raise RegistryError(
                f"deployment is {row['status']} and cannot receive an image"
            )
        if row["revision"] not in (None, publication.tag):
            raise ImageIdentityConflict(publication.digest)
        if row["image_digest"] not in (None, publication.digest):
            raise ImageIdentityConflict(publication.digest)
        await self.connection.execute(
            """
            UPDATE deployments
               SET revision = $2,
                   image_digest = $3,
                   stage = 'pushed'
             WHERE id = $1
            """,
            publication.deployment_id,
            publication.tag,
            publication.digest,
        )

    async def _org_limit(self, org_id: str) -> int:
        row = await self.connection.fetchrow(
            """
            SELECT p.limits
              FROM orgs o
              LEFT JOIN plans p ON p.id = o.plan_id
             WHERE o.id = $1
            """,
            org_id,
        )
        if row is None:
            raise RegistryError("organisation does not exist")
        return _storage_limit_bytes(row["limits"])

    async def _write_usage(self, org_id: str, day: date) -> None:
        total = await self.connection.fetchval(
            "SELECT COALESCE(SUM(size_bytes), 0)::bigint FROM images WHERE org_id = $1",
            org_id,
        )
        await self.connection.execute(
            """
            INSERT INTO usage_daily (org_id, day, storage_bytes)
            VALUES ($1, $2, $3)
            ON CONFLICT (org_id, day)
            DO UPDATE SET storage_bytes = EXCLUDED.storage_bytes
            """,
            org_id,
            day,
            int(total or 0),
        )


class PostgresImageGarbageCollector:
    """Two-phase retention for images.

    ``mark`` and ``delete_marked`` are intentionally separate entry points.
    A scheduler calls mark nightly and delete on a later run; deleting in the
    same invocation would defeat the reference re-check that makes R-7 safe.
    """

    def __init__(
        self,
        connection: asyncpg.Connection,
        registry: ContainerRegistry,
        *,
        clock=_utcnow,
        recent_window_days: int = 30,
        superseded_window_days: int = 14,
        orphan_window_days: int = 7,
    ) -> None:
        if min(recent_window_days, superseded_window_days, orphan_window_days) <= 0:
            raise ValueError("GC retention windows must be positive")
        self.connection = connection
        self.registry = registry
        self._clock = clock
        self.recent_window = timedelta(days=recent_window_days)
        self.superseded_window = timedelta(days=superseded_window_days)
        self.orphan_window = timedelta(days=orphan_window_days)

    async def mark(self, *, now: datetime | None = None) -> GcReport:
        at = _aware(now or self._clock())
        recent = at - self.recent_window
        superseded = at - self.superseded_window
        orphan = at - self.orphan_window
        async with self.connection.transaction():
            unmarked = await self._unmark_route_referenced()
            rows = await self.connection.fetch(
                """
                UPDATE images i
                   SET marked_for_deletion_at = $4
                 WHERE i.marked_for_deletion_at IS NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM routes r WHERE r.revision = i.tag
                   )
                   AND (
                       i.created_at < $1
                       OR (i.deployment_id IS NULL AND i.created_at < $3)
                       OR EXISTS (
                           SELECT 1
                             FROM deployments d
                            WHERE d.id = i.deployment_id
                              AND d.status = 'superseded'
                              AND COALESCE(d.finished_at, i.created_at) < $2
                       )
                   )
                RETURNING i.id
                """,
                recent,
                superseded,
                orphan,
                at,
            )
        return GcReport(marked=len(rows), unmarked=unmarked)

    async def delete_marked(self, *, now: datetime | None = None) -> GcReport:
        at = _aware(now or self._clock())
        cutoff = at - timedelta(days=7)
        errors: list[str] = []
        deleted = 0
        retained = 0
        touched_orgs: set[str] = set()
        async with self.connection.transaction():
            unmarked = await self._unmark_route_referenced()
            candidates = await self.connection.fetch(
                """
                SELECT i.id, i.org_id, i.tag, i.digest, i.size_bytes,
                       o.slug AS org_slug, p.slug AS project_slug
                  FROM images i
                  JOIN orgs o ON o.id = i.org_id
                  JOIN projects p ON p.id = i.project_id
                 WHERE i.marked_for_deletion_at IS NOT NULL
                   AND i.marked_for_deletion_at < $1
                   AND NOT EXISTS (
                       SELECT 1 FROM routes r WHERE r.revision = i.tag
                   )
                 ORDER BY i.marked_for_deletion_at
                 FOR UPDATE OF i SKIP LOCKED
                """,
                cutoff,
            )
            for row in candidates:
                org_id = str(row["org_id"])
                referenced = await self.connection.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM routes WHERE revision = $1)",
                    row["tag"],
                )
                if referenced:
                    await self._unmark(row["id"])
                    unmarked += 1
                    retained += 1
                    continue
                repository = (
                    f"{REGISTRY_NAMESPACE}/{row['org_slug']}/{row['project_slug']}"
                )
                try:
                    await self.registry.delete(
                        repository=repository, reference=row["tag"]
                    )
                except Exception as exc:
                    errors.append(
                        f"{repository}:{row['tag']}: {type(exc).__name__}"
                    )
                    retained += 1
                    continue
                result = await self.connection.execute(
                    """
                    DELETE FROM images
                     WHERE id = $1
                       AND NOT EXISTS (
                           SELECT 1 FROM routes WHERE revision = $2
                       )
                    """,
                    row["id"],
                    row["tag"],
                )
                if result == "DELETE 1":
                    deleted += 1
                    touched_orgs.add(org_id)
                else:
                    await self._unmark(row["id"])
                    retained += 1
            for org_id in touched_orgs:
                await self._write_usage(org_id, at.date())
        return GcReport(
            unmarked=unmarked,
            deleted=deleted,
            retained=retained,
            errors=tuple(errors),
        )

    async def _unmark_route_referenced(self) -> int:
        result = await self.connection.execute(
            """
            UPDATE images i
               SET marked_for_deletion_at = NULL
             WHERE i.marked_for_deletion_at IS NOT NULL
               AND EXISTS (SELECT 1 FROM routes r WHERE r.revision = i.tag)
            """
        )
        return _row_count(result)

    async def _unmark(self, image_id) -> None:
        await self.connection.execute(
            "UPDATE images SET marked_for_deletion_at = NULL WHERE id = $1",
            image_id,
        )

    async def _write_usage(self, org_id: str, day: date) -> None:
        total = await self.connection.fetchval(
            "SELECT COALESCE(SUM(size_bytes), 0)::bigint FROM images WHERE org_id = $1",
            org_id,
        )
        await self.connection.execute(
            """
            INSERT INTO usage_daily (org_id, day, storage_bytes)
            VALUES ($1, $2, $3)
            ON CONFLICT (org_id, day)
            DO UPDATE SET storage_bytes = EXCLUDED.storage_bytes
            """,
            org_id,
            day,
            int(total or 0),
        )


PostgresGC = PostgresImageGarbageCollector
PostgresImageGC = PostgresImageGarbageCollector


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _row_count(result: str) -> int:
    try:
        return int(result.rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


__all__ = [
    "GcReport",
    "PostgresGC",
    "PostgresImageGC",
    "PostgresImageGarbageCollector",
    "PostgresImageStore",
]
