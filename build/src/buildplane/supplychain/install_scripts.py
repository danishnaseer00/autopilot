"""Install-script exceptions (SPEC-10 R-2..R-4).

The safe default is always ``--ignore-scripts``. A project may grant one package
an exception; that exception is persisted in the existing
``build_script_exceptions`` table and written to ``audit_log`` before it takes
effect. The build command then runs ``<pm> rebuild <package>`` for that package
only. The container baseline never changes.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Mapping, Protocol

import asyncpg

from buildplane.supplychain.audit import (
    ACTION_INSTALL_SCRIPT_GRANT,
    AuditLog,
    AuditRecord,
)
from buildplane.supplychain.models import LockedPackage, validate_package_name


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@dataclass(frozen=True, slots=True)
class BuildScriptException:
    project_id: str
    package_name: str
    granted_by: str
    reason: str
    expires_at: datetime | None
    granted_at: datetime | None = None


class BuildScriptExceptionStore(Protocol):
    async def active(
        self,
        *,
        org_id: str,
        project_id: str,
        at: datetime,
    ) -> Mapping[str, datetime | None]: ...

    async def grant(
        self,
        *,
        org_id: str,
        project_id: str,
        package_name: str,
        granted_by: str,
        reason: str,
        expires_at: datetime | None = None,
    ) -> BuildScriptException: ...


@dataclass(frozen=True, slots=True)
class InstallScriptDecision:
    detected: tuple[LockedPackage, ...]
    allowed: tuple[str, ...]
    suppressed: tuple[LockedPackage, ...]


def detect_install_scripts(
    packages: tuple[LockedPackage, ...],
) -> tuple[LockedPackage, ...]:
    return tuple(package for package in packages if package.has_install_script)


def decide_install_scripts(
    packages: tuple[LockedPackage, ...],
    active: Mapping[str, datetime | None],
    *,
    at: datetime,
) -> InstallScriptDecision:
    detected = detect_install_scripts(packages)
    allowed = {
        package.name
        for package in detected
        if package.name in active
        and (active[package.name] is None or active[package.name] > at)
    }
    return InstallScriptDecision(
        detected=detected,
        allowed=tuple(sorted(allowed)),
        suppressed=tuple(package for package in detected if package.name not in allowed),
    )


class InMemoryBuildScriptExceptionStore:
    """Reference store used by tests and local composition."""

    def __init__(self, audit_log: AuditLog, *, clock=_utcnow) -> None:
        self._audit_log = audit_log
        self._clock = clock
        self._rows: dict[tuple[str, str, str], BuildScriptException] = {}

    async def active(
        self,
        *,
        org_id: str,
        project_id: str,
        at: datetime,
    ) -> Mapping[str, datetime | None]:
        result: dict[str, datetime | None] = {}
        for (row_org, row_project, package_name), exception in self._rows.items():
            if row_org != org_id or row_project != project_id:
                continue
            if exception.expires_at is not None and exception.expires_at <= at:
                continue
            result[package_name] = exception.expires_at
        return result

    async def grant(
        self,
        *,
        org_id: str,
        project_id: str,
        package_name: str,
        granted_by: str,
        reason: str,
        expires_at: datetime | None = None,
    ) -> BuildScriptException:
        _validate_grant(package_name, reason, expires_at, now=self._clock())
        exception = BuildScriptException(
            project_id=project_id,
            package_name=package_name,
            granted_by=granted_by,
            reason=reason,
            expires_at=_as_utc(expires_at),
            granted_at=self._clock(),
        )
        # Audit first: a crash after this line leaves an audit row for a
        # permission that was not committed, never the reverse.
        await self._audit_log.write(_audit_record(exception, org_id))
        self._rows[(org_id, project_id, package_name)] = exception
        return exception


class PostgresBuildScriptExceptionStore:
    """Production store over the existing ``build_script_exceptions`` table."""

    def __init__(self, database, audit_log: AuditLog, *, clock=_utcnow) -> None:
        self._database = database
        self._audit_log = audit_log
        self._clock = clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if isinstance(self._database, asyncpg.Pool):
            async with self._database.acquire() as connection:
                yield connection
        else:
            yield self._database

    async def active(
        self,
        *,
        org_id: str,
        project_id: str,
        at: datetime,
    ) -> Mapping[str, datetime | None]:
        async with self._connection() as connection:
            async with connection.transaction():
                await _set_org(connection, org_id)
                rows = await connection.fetch(
                    """
                    SELECT package_name, expires_at
                    FROM build_script_exceptions
                    WHERE project_id = $1
                      AND (expires_at IS NULL OR expires_at > $2)
                    """,
                    _uuid(project_id),
                    at,
                )
        return {row["package_name"]: row["expires_at"] for row in rows}

    async def grant(
        self,
        *,
        org_id: str,
        project_id: str,
        package_name: str,
        granted_by: str,
        reason: str,
        expires_at: datetime | None = None,
    ) -> BuildScriptException:
        _validate_grant(package_name, reason, expires_at, now=self._clock())
        exception = BuildScriptException(
            project_id=project_id,
            package_name=package_name,
            granted_by=granted_by,
            reason=reason,
            expires_at=_as_utc(expires_at),
            granted_at=self._clock(),
        )
        async with self._connection() as connection:
            async with connection.transaction():
                await _set_org(connection, org_id)
                await connection.execute(
                    """
                    INSERT INTO build_script_exceptions
                      (project_id, package_name, granted_by, reason, expires_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (project_id, package_name) DO UPDATE
                    SET granted_by = EXCLUDED.granted_by,
                        reason = EXCLUDED.reason,
                        expires_at = EXCLUDED.expires_at,
                        granted_at = now()
                    """,
                    _uuid(project_id),
                    package_name,
                    _uuid(granted_by),
                    reason,
                    exception.expires_at,
                )
                # The audit row is inside the transaction attempt: if it fails,
                # the exception is rolled back. If the commit fails after the
                # audit write, the audit row over-records an attempted grant,
                # which is the safe direction.
                await self._audit_log.write(_audit_record(exception, org_id))
        return exception


async def _set_org(connection: asyncpg.Connection, org_id: str) -> None:
    # The table is not RLS-protected in the ratified schema (finding logged in
    # DECISIONS.md), but setting the context here means the store keeps working
    # when the coordinator adds the missing policy.
    await connection.execute(
        "SELECT set_config('app.org_id', $1, true)",
        str(_uuid(org_id)),
    )


def _validate_grant(
    package_name: str,
    reason: str,
    expires_at: datetime | None,
    *,
    now: datetime,
) -> None:
    validate_package_name(package_name)
    if not reason or not reason.strip():
        raise ValueError("reason is required")
    if expires_at is not None and _as_utc(expires_at) <= _as_utc(now):
        raise ValueError("expires_at must be in the future")


def _audit_record(
    exception: BuildScriptException,
    org_id: str,
) -> AuditRecord:
    return AuditRecord(
        action=ACTION_INSTALL_SCRIPT_GRANT,
        actor_type="user" if exception.granted_by else "system",
        actor_id=exception.granted_by or "build-plane",
        org_id=org_id,
        target_type="project",
        target_id=exception.project_id,
        metadata={
            "project_id": exception.project_id,
            "package_name": exception.package_name,
            "reason": exception.reason,
            "expires_at": (
                exception.expires_at.isoformat()
                if exception.expires_at is not None
                else None
            ),
        },
    )
