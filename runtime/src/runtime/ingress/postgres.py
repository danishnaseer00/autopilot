"""Postgres adapters for SPEC-15.

These adapters intentionally use a platform-runtime connection.  The ingress
source and certificate ledger are platform-owned (the migration revokes the
tenant role's grants); ``PostgresDomainStore`` is called inside the caller's
tenant transaction with the normal RLS context.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

import asyncpg

from platform_shared.manifest.models import ManifestDocument
from runtime.ingress.certificates import CertificateStore
from runtime.ingress.domains import DomainStore
from runtime.ingress.errors import (
    CertificateBudgetExceeded,
    CertificateNotFound,
    DomainAlreadyClaimed,
    DomainNotFound,
    InvalidDomainState,
    OutboxError,
)
from runtime.ingress.models import (
    CertificateRecord,
    DomainRecord,
    IngressSnapshot,
    IssuedCertificate,
    OutboxMessage,
    RouteRule,
    certificate_from_row,
    domain_from_row,
    route_from_row,
)
from runtime.ingress.outbox import OutboxStore
from runtime.ingress.validation import canonical_hostname
from runtime.k8srender.names import object_name


def _as_dict(row: asyncpg.Record | Mapping[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value
    return value


class PostgresIngressSource:
    """Read the complete route/domain snapshot for one reconciliation poll."""

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def snapshot(self) -> IngressSnapshot:
        route_rows = await self._conn.fetch(
            """
            SELECT r.id, r.org_id, r.project_id, r.hostname, r.path_prefix,
                   r.kind::text AS kind, r.target_path, r.status_code,
                   r.priority, r.deployment_id, r.revision,
                   p.slug AS project_slug, p.k8s_namespace AS namespace,
                   d.manifest
              FROM routes r
              JOIN projects p ON p.id = r.project_id
              LEFT JOIN deployments d ON d.id = r.deployment_id
             ORDER BY r.hostname, r.path_prefix, r.id
            """
        )
        domain_rows = await self._conn.fetch(
            """
            SELECT id, org_id, project_id, hostname, is_production, status,
                   verification_token, cert_issued_at, cert_expires_at,
                   last_checked_at
              FROM domains
             ORDER BY hostname
            """
        )
        certificate_rows = await self._conn.fetch(
            """
            SELECT id, org_id, registered_domain, hostnames, is_wildcard,
                   status, idempotency_key, issuer, serial, issued_at,
                   expires_at, revoked_at, last_error, created_at
              FROM ingress_certificates
             ORDER BY registered_domain, created_at DESC
            """
        )

        routes: list[RouteRule] = []
        for raw in route_rows:
            row = dict(raw)
            route = route_from_row(row)
            if route.kind in {"proxy", "static"}:
                backend = _backend_from_manifest(
                    row.get("manifest"),
                    route.path_prefix,
                    project_slug=row.get("project_slug"),
                    project_id=row["project_id"],
                )
                if backend is not None:
                    route = RouteRule(
                        **{
                            **route.__dict__,
                            "namespace": row["namespace"],
                            "service_name": backend[0],
                            "service_port": backend[1],
                        }
                    )
            else:
                route = RouteRule(
                    **{**route.__dict__, "namespace": row["namespace"]}
                )
            routes.append(route)

        return IngressSnapshot(
            routes=tuple(routes),
            domains=tuple(domain_from_row(dict(row)) for row in domain_rows),
            certificates=tuple(
                certificate_from_row(dict(row)) for row in certificate_rows
            ),
        )


def _backend_from_manifest(
    manifest: Any,
    path_prefix: str,
    *,
    project_slug: str | None,
    project_id: str,
) -> tuple[str, int] | None:
    """Resolve a route path to the service the deployment manifest names.

    The routes table stores the host/path cutover, not a service name.  The
    immutable deployment manifest is the source of truth for which service owns
    that path.  A manifest that cannot be parsed is a hard skip: an Ingress must
    never be pointed at a guessed service.
    """

    parsed = _json_value(manifest)
    if not isinstance(parsed, Mapping):
        return None
    try:
        document = ManifestDocument.model_validate(parsed)
    except Exception:
        return None
    exposure = document.exposure
    if exposure is None or not exposure.routes:
        return None

    candidates = [route for route in exposure.routes if route.path == path_prefix]
    if not candidates:
        candidates = [
            route
            for route in exposure.routes
            if path_prefix.startswith(route.path.rstrip("/") or "/")
        ]
        if candidates:
            prefix = max(candidates, key=lambda route: len(route.path))
            candidates = [prefix]
    if not candidates:
        return None

    service_name = candidates[0].service
    service = next(
        (entry for entry in document.services if entry.name == service_name), None
    )
    if service is None:
        return None
    try:
        name = object_name(project_slug, project_id, service_name)
    except Exception:
        return None
    return name, service.runtime.port


class PostgresCertificateStore(CertificateStore):
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def get(self, certificate_id: str) -> CertificateRecord | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM ingress_certificates WHERE id = $1",
            _uuid(certificate_id),
        )
        data = _as_dict(row)
        return certificate_from_row(data) if data else None

    async def active_for(
        self,
        *,
        registered_domain: str,
        hostnames: Sequence[str],
        is_wildcard: bool,
    ) -> CertificateRecord | None:
        row = await self._conn.fetchrow(
            """
            SELECT *
              FROM ingress_certificates
             WHERE registered_domain = $1
               AND is_wildcard = $2
               AND status IN ('pending','issued')
               AND hostnames @> $3::text[]
             ORDER BY created_at DESC
             LIMIT 1
            """,
            registered_domain,
            is_wildcard,
            list(hostnames),
        )
        data = _as_dict(row)
        return certificate_from_row(data) if data else None

    async def reserve(
        self,
        *,
        org_id: str | None,
        registered_domain: str,
        hostnames: Sequence[str],
        is_wildcard: bool,
        idempotency_key: str,
        issuer: str,
        now: datetime,
        window: timedelta,
        limit: int,
    ) -> CertificateRecord:
        async with self._conn.transaction():
            # Serialise budget checks for one registered domain.  The unique
            # wildcard index is the second line of defence for concurrent
            # issuance attempts.
            await self._conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1)::bigint)",
                registered_domain,
            )
            existing = await self._conn.fetchrow(
                "SELECT * FROM ingress_certificates WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is not None:
                return certificate_from_row(dict(existing))

            cutoff = now - window
            used = await self._conn.fetchval(
                """
                SELECT count(*)
                  FROM ingress_certificates
                 WHERE registered_domain = $1
                   AND created_at >= $2
                """,
                registered_domain,
                cutoff,
            )
            if int(used) >= limit:
                raise CertificateBudgetExceeded(
                    registered_domain, limit=limit, window=window, used=int(used)
                )

            row = await self._conn.fetchrow(
                """
                INSERT INTO ingress_certificates
                    (org_id, registered_domain, hostnames, is_wildcard,
                     status, idempotency_key, issuer, created_at, updated_at)
                VALUES ($1, $2, $3, $4, 'pending', $5, $6, $7, $7)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING *
                """,
                _uuid_or_none(org_id),
                registered_domain,
                list(hostnames),
                is_wildcard,
                idempotency_key,
                issuer,
                now,
            )
            if row is None:
                row = await self._conn.fetchrow(
                    "SELECT * FROM ingress_certificates WHERE idempotency_key = $1",
                    idempotency_key,
                )
            if row is None:  # pragma: no cover - defensive
                raise OutboxError("certificate reservation disappeared")
            if org_id is not None:
                await self._conn.execute(
                    """
                    INSERT INTO usage_daily (org_id, day, certificates_issued)
                    VALUES ($1, $2, 1)
                    ON CONFLICT (org_id, day)
                    DO UPDATE SET certificates_issued =
                        usage_daily.certificates_issued + 1
                    """,
                    _uuid(org_id),
                    now.date(),
                )
            return certificate_from_row(dict(row))

    async def mark_issued(
        self, certificate_id: str, issued: IssuedCertificate
    ) -> CertificateRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE ingress_certificates
               SET status = 'issued', serial = $2, issued_at = $3,
                   expires_at = $4, last_error = NULL, updated_at = now()
             WHERE id = $1
             RETURNING *
            """,
            _uuid(certificate_id),
            issued.serial,
            issued.issued_at,
            issued.expires_at,
        )
        if row is None:
            raise CertificateNotFound(certificate_id)
        certificate = certificate_from_row(dict(row))
        if not certificate.is_wildcard:
            await self._conn.execute(
                """
                UPDATE domains
                   SET status = 'active', cert_issued_at = $2,
                       cert_expires_at = $3, last_checked_at = now()
                 WHERE hostname = ANY($1::text[])
                   AND status IN ('verified','active')
                """,
                list(certificate.hostnames),
                issued.issued_at,
                issued.expires_at,
            )
        return certificate

    async def mark_failed(
        self, certificate_id: str, error: str, now: datetime
    ) -> CertificateRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE ingress_certificates
               SET status = 'failed', last_error = $2, updated_at = $3
             WHERE id = $1
             RETURNING *
            """,
            _uuid(certificate_id),
            error[:500],
            now,
        )
        if row is None:
            raise CertificateNotFound(certificate_id)
        return certificate_from_row(dict(row))

    async def revoke(self, certificate_id: str, now: datetime) -> CertificateRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE ingress_certificates
               SET status = 'revoked', revoked_at = $2, updated_at = $2
             WHERE id = $1
             RETURNING *
            """,
            _uuid(certificate_id),
            now,
        )
        if row is None:
            raise CertificateNotFound(certificate_id)
        return certificate_from_row(dict(row))

    async def live(self) -> tuple[CertificateRecord, ...]:
        rows = await self._conn.fetch(
            """
            SELECT * FROM ingress_certificates
             WHERE status IN ('pending','issued')
             ORDER BY created_at, id
            """
        )
        return tuple(certificate_from_row(dict(row)) for row in rows)


class PostgresDomainStore(DomainStore):
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def find(self, hostname: str) -> DomainRecord | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM domains WHERE hostname = $1", canonical_hostname(hostname)
        )
        data = _as_dict(row)
        return domain_from_row(data) if data else None

    async def create(self, domain: DomainRecord) -> DomainRecord:
        row = await self._conn.fetchrow(
            """
            INSERT INTO domains
                (id, org_id, project_id, hostname, is_production, status,
                 verification_token, cert_issued_at, cert_expires_at,
                 last_checked_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
            ON CONFLICT (hostname) DO NOTHING
            RETURNING *
            """,
            _uuid(domain.id),
            _uuid(domain.org_id),
            _uuid(domain.project_id),
            domain.hostname,
            domain.is_production,
            domain.status,
            domain.verification_token,
            domain.cert_issued_at,
            domain.cert_expires_at,
            domain.last_checked_at,
        )
        if row is None:
            existing = await self.find(domain.hostname)
            if existing is None:  # pragma: no cover - defensive
                raise DomainAlreadyClaimed(domain.hostname)
            if (
                existing.org_id != domain.org_id
                or existing.project_id != domain.project_id
            ):
                raise DomainAlreadyClaimed(
                    f"hostname {domain.hostname!r} is already owned by another project"
                )
            return existing
        return domain_from_row(dict(row))

    async def update_status(
        self, domain_id: str, status: str, *, now: datetime
    ) -> DomainRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE domains
               SET status = $2, last_checked_at = $3
             WHERE id = $1
             RETURNING *
            """,
            _uuid(domain_id),
            status,
            now,
        )
        if row is None:
            raise DomainNotFound(domain_id)
        return domain_from_row(dict(row))

    async def activate(
        self, domain_id: str, *, issued: IssuedCertificate
    ) -> DomainRecord:
        row = await self._conn.fetchrow(
            """
            UPDATE domains
               SET status = 'active', cert_issued_at = $2,
                   cert_expires_at = $3, last_checked_at = now()
             WHERE id = $1 AND status IN ('verified','active')
             RETURNING *
            """,
            _uuid(domain_id),
            issued.issued_at,
            issued.expires_at,
        )
        if row is None:
            existing = await self._conn.fetchrow(
                "SELECT * FROM domains WHERE id = $1", _uuid(domain_id)
            )
            if existing is None:
                raise DomainNotFound(domain_id)
            raise InvalidDomainState(
                f"domain {domain_id} is not in a verifiable state for activation"
            )
        return domain_from_row(dict(row))

    async def list_all(self) -> tuple[DomainRecord, ...]:
        rows = await self._conn.fetch("SELECT * FROM domains ORDER BY hostname")
        return tuple(domain_from_row(dict(row)) for row in rows)

    async def delete(self, hostname: str) -> None:
        result = await self._conn.execute(
            "DELETE FROM domains WHERE hostname = $1", canonical_hostname(hostname)
        )
        if result.endswith(" 0"):
            raise DomainNotFound(hostname)

    async def upsert_route(self, route: RouteRule) -> RouteRule:
        row = await self._conn.fetchrow(
            """
            INSERT INTO routes
                (id, org_id, project_id, hostname, path_prefix, deployment_id,
                 revision, kind, target_path, status_code, priority)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (hostname, path_prefix) DO NOTHING
            RETURNING *
            """,
            _uuid(route.id),
            _uuid(route.org_id),
            _uuid(route.project_id),
            route.hostname,
            route.path_prefix,
            _uuid_or_none(route.deployment_id),
            route.revision,
            route.kind,
            route.target_path,
            route.status_code,
            route.priority,
        )
        if row is None:
            existing = await self._conn.fetchrow(
                "SELECT * FROM routes WHERE hostname = $1 AND path_prefix = $2",
                route.hostname,
                route.path_prefix,
            )
            if existing is None:  # pragma: no cover - defensive
                raise DomainAlreadyClaimed(route.hostname)
            if str(existing["project_id"]) != str(route.project_id):
                raise DomainAlreadyClaimed(
                    f"route {route.hostname}{route.path_prefix} is owned by another project"
                )
            data = dict(existing)
            return route_from_row({**data, "namespace": route.namespace})
        return route_from_row({**dict(row), "namespace": route.namespace})

    async def route_for(self, hostname: str) -> RouteRule | None:
        row = await self._conn.fetchrow(
            """
            SELECT * FROM routes
             WHERE hostname = $1 AND path_prefix = '/'
             LIMIT 1
            """,
            canonical_hostname(hostname),
        )
        data = _as_dict(row)
        return route_from_row(data) if data else None

    async def delete_routes(self, hostname: str) -> int:
        result = await self._conn.execute(
            "DELETE FROM routes WHERE hostname = $1", canonical_hostname(hostname)
        )
        return int(result.rsplit(" ", 1)[-1])


class PostgresOutbox(OutboxStore):
    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn

    async def enqueue(
        self,
        topic: str,
        idempotency_key: str,
        payload: Mapping[str, Any],
        *,
        now: datetime,
        available_at: datetime | None = None,
        max_attempts: int = 8,
    ) -> OutboxMessage:
        if not topic or not idempotency_key:
            raise OutboxError("outbox topic and idempotency key are required")
        if max_attempts < 1:
            raise OutboxError("max_attempts must be positive")
        row = await self._conn.fetchrow(
            """
            INSERT INTO ingress_outbox
                (topic, idempotency_key, payload, status, attempts,
                 max_attempts, available_at, created_at, updated_at)
            VALUES ($1, $2, $3::jsonb, 'pending', 0, $4, $5, $6, $6)
            ON CONFLICT (topic, idempotency_key) DO NOTHING
            RETURNING *
            """,
            topic,
            idempotency_key,
            json.dumps(dict(payload), separators=(",", ":")),
            max_attempts,
            available_at or now,
            now,
        )
        if row is None:
            row = await self._conn.fetchrow(
                """
                SELECT * FROM ingress_outbox
                 WHERE topic = $1 AND idempotency_key = $2
                """,
                topic,
                idempotency_key,
            )
        if row is None:  # pragma: no cover - defensive
            raise OutboxError("outbox enqueue disappeared")
        return _outbox_from_row(dict(row))

    async def claim(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> OutboxMessage | None:
        if lease_seconds < 1:
            raise OutboxError("lease_seconds must be positive")
        row = await self._conn.fetchrow(
            """
            WITH candidate AS (
                SELECT id
                  FROM ingress_outbox
                 WHERE (status = 'pending' AND available_at <= $1)
                    OR (status = 'running'
                        AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= $1)
                 ORDER BY available_at, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT 1
            )
            UPDATE ingress_outbox o
               SET status = 'running',
                   attempts = o.attempts + 1,
                   worker_id = $2,
                   lease_expires_at = $1 + make_interval(secs => $3),
                   updated_at = $1
              FROM candidate
             WHERE o.id = candidate.id
             RETURNING o.*
            """,
            now,
            worker_id,
            lease_seconds,
        )
        return _outbox_from_row(dict(row)) if row is not None else None

    async def complete(
        self, message_id: str, *, worker_id: str, now: datetime
    ) -> OutboxMessage:
        row = await self._conn.fetchrow(
            """
            UPDATE ingress_outbox
               SET status = 'succeeded', worker_id = NULL,
                   lease_expires_at = NULL, updated_at = $3
             WHERE id = $1 AND status = 'running' AND worker_id = $2
             RETURNING *
            """,
            _uuid(message_id),
            worker_id,
            now,
        )
        if row is None:
            raise OutboxError(f"outbox message {message_id} is not owned by worker")
        return _outbox_from_row(dict(row))

    async def retry(
        self,
        message_id: str,
        *,
        worker_id: str,
        error: str,
        now: datetime,
        available_at: datetime,
    ) -> OutboxMessage:
        row = await self._conn.fetchrow(
            """
            UPDATE ingress_outbox
               SET status = CASE WHEN attempts >= max_attempts
                                 THEN 'dead' ELSE 'pending' END,
                   worker_id = NULL,
                   lease_expires_at = NULL,
                   last_error = $3,
                   available_at = $4,
                   updated_at = $5
             WHERE id = $1 AND status = 'running' AND worker_id = $2
             RETURNING *
            """,
            _uuid(message_id),
            worker_id,
            error[:500],
            available_at,
            now,
        )
        if row is None:
            raise OutboxError(f"outbox message {message_id} is not owned by worker")
        return _outbox_from_row(dict(row))

    async def get(self, message_id: str) -> OutboxMessage | None:
        row = await self._conn.fetchrow(
            "SELECT * FROM ingress_outbox WHERE id = $1", _uuid(message_id)
        )
        return _outbox_from_row(dict(row)) if row is not None else None


def _outbox_from_row(row: Mapping[str, Any]) -> OutboxMessage:
    payload = _json_value(row["payload"])
    if not isinstance(payload, Mapping):
        payload = {}
    return OutboxMessage(
        id=str(row["id"]),
        topic=str(row["topic"]),
        idempotency_key=str(row["idempotency_key"]),
        payload=dict(payload),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        available_at=row["available_at"],
        lease_expires_at=row.get("lease_expires_at"),
        worker_id=row.get("worker_id"),
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
    )


def _uuid(value: str | uuid.UUID) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{value!r} is not a UUID") from exc


def _uuid_or_none(value: str | uuid.UUID | None) -> uuid.UUID | None:
    return _uuid(value) if value is not None else None


# Kept close to the adapter rather than exported from the public package.
__all__ = [
    "PostgresCertificateStore",
    "PostgresDomainStore",
    "PostgresIngressSource",
    "PostgresOutbox",
]
