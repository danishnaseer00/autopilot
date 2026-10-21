"""Audit-log access for SPEC-10.

``audit_log`` already exists in the separate audit database (SPEC-03 §3); this
module deliberately does **not** add a second audit store or any table. The
build plane owns the writer/query adapter because the runtime copy is a
different process with a different DSN.

Every write is synchronous with the decision it records: if the audit write
fails, the build fails closed. That is the same rule the runtime secret path
uses (SPEC-02 R-9), and it is what makes an install-script or image-scan
exception trustworthy.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping, Protocol

import asyncpg

from buildplane.supplychain.errors import AuditWriteFailed

ACTION_LOCKFILE_OBSERVED = "build.lockfile_observed"
ACTION_LOCKFILE_CHANGED = "build.lockfile_changed"
ACTION_INSTALL_SCRIPT_GRANT = "build.install_script_exception_grant"
ACTION_INSTALL_SCRIPT_USED = "build.install_script_exception_used"
ACTION_IMAGE_SCAN_EXCEPTION = "image.scan_exception"
ACTION_IMAGE_SCAN_EXCEPTION_USED = "image.scan_exception_used"
ACTION_REGISTRY_PULL = "registry.pull"
ACTION_REGISTRY_DENIED = "registry.denied"
ACTION_REGISTRY_FAILOVER = "registry.failover"
ACTION_EGRESS_DENIED = "build.egress_denied"
ACTION_PROVENANCE = "build.provenance"

_ACTOR_TYPES = frozenset({"user", "agent", "system"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


@dataclass(frozen=True, slots=True)
class AuditRecord:
    action: str
    actor_type: str = "system"
    actor_id: str = "build-plane"
    org_id: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.action or "." not in self.action:
            raise ValueError("audit action must be a dotted verb")
        if self.actor_type not in _ACTOR_TYPES:
            raise ValueError(f"invalid audit actor_type: {self.actor_type!r}")
        if not self.actor_id:
            raise ValueError("audit actor_id is required")
        if self.org_id is not None:
            object.__setattr__(self, "org_id", str(self.org_id))
        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True, slots=True)
class AffectedTenant:
    """One tenant/build that pulled or was denied a package version."""

    org_id: str | None
    project_id: str | None
    build_id: str | None
    action: str


class AuditLog(Protocol):
    async def write(self, record: AuditRecord) -> None: ...

    async def previous_lockfile_hash(
        self,
        *,
        org_id: str,
        project_id: str,
        git_sha: str,
        lockfile_name: str,
    ) -> str | None: ...

    async def active_image_exception(
        self,
        *,
        org_id: str,
        image: str,
        at: datetime,
    ) -> Mapping[str, Any] | None: ...

    async def affected_tenants(
        self,
        *,
        incident_id: str | None = None,
        package: str | None = None,
        version: str | None = None,
    ) -> tuple[AffectedTenant, ...]: ...


class InMemoryAuditLog:
    """Reference implementation used by unit tests and local composition."""

    def __init__(
        self,
        *,
        fail_writes: bool = False,
        fail_queries: bool = False,
        clock=_utcnow,
    ) -> None:
        self.rows: list[AuditRecord] = []
        self.fail_writes = fail_writes
        self.fail_queries = fail_queries
        self._clock = clock

    async def write(self, record: AuditRecord) -> None:
        if self.fail_writes:
            raise AuditWriteFailed()
        self.rows.append(record)

    async def previous_lockfile_hash(
        self,
        *,
        org_id: str,
        project_id: str,
        git_sha: str,
        lockfile_name: str,
    ) -> str | None:
        if self.fail_queries:
            raise AuditWriteFailed("audit query failed")
        for record in reversed(self.rows):
            if record.action != ACTION_LOCKFILE_OBSERVED:
                continue
            metadata = record.metadata
            if (
                record.org_id == org_id
                and metadata.get("project_id") == project_id
                and metadata.get("git_sha") == git_sha
                and metadata.get("lockfile_name") == lockfile_name
            ):
                value = metadata.get("lockfile_hash")
                return value if isinstance(value, str) else None
        return None

    async def active_image_exception(
        self,
        *,
        org_id: str,
        image: str,
        at: datetime,
    ) -> Mapping[str, Any] | None:
        if self.fail_queries:
            raise AuditWriteFailed("audit query failed")
        for record in reversed(self.rows):
            if record.action != ACTION_IMAGE_SCAN_EXCEPTION or record.org_id != org_id:
                continue
            if record.metadata.get("image") != image:
                continue
            expires_at = _parse_time(record.metadata.get("expires_at"))
            if expires_at is not None and expires_at > at:
                return record.metadata
        return None

    async def affected_tenants(
        self,
        *,
        incident_id: str | None = None,
        package: str | None = None,
        version: str | None = None,
    ) -> tuple[AffectedTenant, ...]:
        if self.fail_queries:
            raise AuditWriteFailed("audit query failed")
        if incident_id is None and (package is None or version is None):
            raise ValueError("incident_id or package+version is required")
        found: set[AffectedTenant] = set()
        for record in self.rows:
            if record.action not in (ACTION_REGISTRY_PULL, ACTION_REGISTRY_DENIED):
                continue
            metadata = record.metadata
            matches_incident = (
                incident_id is not None
                and metadata.get("incident_id") == incident_id
            )
            matches_package = (
                package is not None
                and version is not None
                and metadata.get("package") == package
                and metadata.get("version") == version
            )
            if not (matches_incident or matches_package):
                continue
            found.add(
                AffectedTenant(
                    org_id=record.org_id,
                    project_id=_as_str(metadata.get("project_id")),
                    build_id=_as_str(metadata.get("build_id")),
                    action=record.action,
                )
            )
        return tuple(sorted(found, key=lambda item: (
            item.org_id or "",
            item.project_id or "",
            item.build_id or "",
            item.action,
        )))


class AsyncpgAuditLog:
    """Production adapter for the separate ``autopilot_audit`` database."""

    def __init__(self, database) -> None:
        self._database = database

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if isinstance(self._database, asyncpg.Pool):
            async with self._database.acquire() as connection:
                yield connection
        else:
            yield self._database

    async def write(self, record: AuditRecord) -> None:
        try:
            async with self._connection() as connection:
                await connection.execute(
                    """
                    INSERT INTO audit_log
                      (org_id, actor_type, actor_id, action, target_type,
                       target_id, metadata)
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    _uuid_or_none(record.org_id),
                    record.actor_type,
                    record.actor_id,
                    record.action,
                    record.target_type,
                    record.target_id,
                    json.dumps(dict(record.metadata), sort_keys=True, default=str),
                )
        except Exception as exc:
            raise AuditWriteFailed(
                f"audit log write failed: {type(exc).__name__}"
            ) from exc

    async def previous_lockfile_hash(
        self,
        *,
        org_id: str,
        project_id: str,
        git_sha: str,
        lockfile_name: str,
    ) -> str | None:
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT metadata->>'lockfile_hash' AS lockfile_hash
                    FROM audit_log
                    WHERE action = $1
                      AND org_id = $2
                      AND metadata->>'project_id' = $3
                      AND metadata->>'git_sha' = $4
                      AND metadata->>'lockfile_name' = $5
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    ACTION_LOCKFILE_OBSERVED,
                    _uuid_or_none(org_id),
                    project_id,
                    git_sha,
                    lockfile_name,
                )
        except Exception as exc:
            raise AuditWriteFailed(
                f"audit lockfile query failed: {type(exc).__name__}"
            ) from exc
        if row is None or row["lockfile_hash"] is None:
            return None
        return str(row["lockfile_hash"])

    async def active_image_exception(
        self,
        *,
        org_id: str,
        image: str,
        at: datetime,
    ) -> Mapping[str, Any] | None:
        try:
            async with self._connection() as connection:
                row = await connection.fetchrow(
                    """
                    SELECT metadata
                    FROM audit_log
                    WHERE action = $1
                      AND org_id = $2
                      AND metadata->>'image' = $3
                      AND metadata ? 'expires_at'
                      AND (metadata->>'expires_at')::timestamptz > $4
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    ACTION_IMAGE_SCAN_EXCEPTION,
                    _uuid_or_none(org_id),
                    image,
                    at,
                )
        except Exception as exc:
            raise AuditWriteFailed(
                f"audit image-exception query failed: {type(exc).__name__}"
            ) from exc
        if row is None:
            return None
        return _json_object(row["metadata"])

    async def affected_tenants(
        self,
        *,
        incident_id: str | None = None,
        package: str | None = None,
        version: str | None = None,
    ) -> tuple[AffectedTenant, ...]:
        if incident_id is None and (package is None or version is None):
            raise ValueError("incident_id or package+version is required")
        try:
            async with self._connection() as connection:
                rows = await connection.fetch(
                    """
                    SELECT org_id,
                           metadata->>'project_id' AS project_id,
                           metadata->>'build_id' AS build_id,
                           action
                    FROM audit_log
                    WHERE action IN ($1, $2)
                      AND (
                        ($3::text IS NOT NULL
                         AND metadata->>'incident_id' = $3)
                        OR
                        ($4::text IS NOT NULL
                         AND metadata->>'package' = $4
                         AND metadata->>'version' = $5)
                      )
                    """,
                    ACTION_REGISTRY_PULL,
                    ACTION_REGISTRY_DENIED,
                    incident_id,
                    package,
                    version,
                )
        except Exception as exc:
            raise AuditWriteFailed(
                f"audit affected-tenant query failed: {type(exc).__name__}"
            ) from exc
        found = {
            AffectedTenant(
                org_id=str(row["org_id"]) if row["org_id"] is not None else None,
                project_id=_as_str(row["project_id"]),
                build_id=_as_str(row["build_id"]),
                action=str(row["action"]),
            )
            for row in rows
        }
        return tuple(sorted(found, key=lambda item: (
            item.org_id or "",
            item.project_id or "",
            item.build_id or "",
            item.action,
        )))


def _json_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise AuditWriteFailed("audit metadata is not a JSON object")


def _as_str(value: Any) -> str | None:
    return None if value is None else str(value)
