"""Data carried across the SPEC-15 ingress/TLS boundary."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from runtime.ingress.errors import InvalidRoute
from runtime.ingress.validation import (
    canonical_hostname,
    validate_hostname_pattern,
    validate_path,
    validate_target,
)

ROUTE_KINDS = frozenset({"proxy", "static", "redirect", "rewrite", "notfound"})
DOMAIN_STATUSES = frozenset({"pending", "verified", "active", "failed", "suspended"})
CERTIFICATE_STATUSES = frozenset({"pending", "issued", "failed", "revoked"})
# ``ratelimited`` is produced by SPEC-16's data-plane facade before route
# resolution.  Keeping it in the shared result type means callers do not need a
# union of two nearly identical response objects.
RESOLUTION_ACTIONS = frozenset(
    {"proxy", "static", "redirect", "notfound", "error", "ratelimited"}
)


@dataclass(frozen=True)
class RouteRule:
    """One row from ``routes`` plus the backend facts needed to render it.

    ``namespace``, ``service_name`` and ``service_port`` are populated by the
    ingress source from the project/deployment; they are deliberately not
    inferred by parsing a hostname.  Redirect/rewrite rows need no backend.
    """

    id: str
    org_id: str
    project_id: str
    hostname: str
    path_prefix: str = "/"
    kind: str = "proxy"
    target_path: str | None = None
    status_code: int = 200
    priority: int = 0
    deployment_id: str | None = None
    revision: str | None = None
    namespace: str | None = None
    service_name: str | None = None
    service_port: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "org_id", str(self.org_id))
        object.__setattr__(self, "project_id", str(self.project_id))
        object.__setattr__(self, "hostname", validate_hostname_pattern(self.hostname))
        object.__setattr__(self, "path_prefix", validate_path(self.path_prefix))
        if self.kind not in ROUTE_KINDS:
            raise InvalidRoute(f"route kind must be one of {sorted(ROUTE_KINDS)}")
        if self.kind in {"redirect", "rewrite"}:
            if not self.target_path:
                raise InvalidRoute(f"{self.kind} route requires target_path")
            object.__setattr__(
                self,
                "target_path",
                validate_target(
                    self.target_path, allow_absolute=self.kind == "redirect"
                ),
            )
        elif self.target_path is not None:
            object.__setattr__(
                self, "target_path", validate_target(self.target_path, allow_absolute=False)
            )
        if self.kind == "redirect" and self.status_code not in {301, 302, 307, 308}:
            raise InvalidRoute("redirect status_code must be 301, 302, 307 or 308")
        if self.kind != "redirect" and self.status_code != 200:
            raise InvalidRoute("only a redirect route may set a non-200 status_code")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int):
            raise InvalidRoute("priority must be an integer")
        if self.service_port is not None and (
            isinstance(self.service_port, bool)
            or not isinstance(self.service_port, int)
            or not 1 <= self.service_port <= 65535
        ):
            raise InvalidRoute("service_port must be 1-65535")

    @property
    def is_terminal(self) -> bool:
        return self.kind != "rewrite"

    @property
    def has_backend(self) -> bool:
        return bool(self.namespace and self.service_name and self.service_port)


@dataclass(frozen=True)
class DomainRecord:
    """A row from ``domains`` mapped into the ingress domain."""

    id: str
    org_id: str
    project_id: str
    hostname: str
    status: str
    is_production: bool = False
    verification_token: str = ""
    cert_issued_at: datetime | None = None
    cert_expires_at: datetime | None = None
    last_checked_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "org_id", str(self.org_id))
        object.__setattr__(self, "project_id", str(self.project_id))
        object.__setattr__(self, "hostname", canonical_hostname(self.hostname))
        if self.status not in DOMAIN_STATUSES:
            raise ValueError(f"domain status must be one of {sorted(DOMAIN_STATUSES)}")

    @property
    def is_active(self) -> bool:
        return self.status == "active" and self.cert_issued_at is not None

    @property
    def can_receive_traffic(self) -> bool:
        return self.is_active


@dataclass(frozen=True)
class CertificateRecord:
    """A durable issuance/budget row (``ingress_certificates``)."""

    id: str
    registered_domain: str
    hostnames: tuple[str, ...]
    is_wildcard: bool
    status: str
    idempotency_key: str
    issuer: str
    org_id: str | None = None
    serial: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(
            self, "hostnames", tuple(validate_hostname_pattern(h) for h in self.hostnames)
        )
        if not self.hostnames:
            raise ValueError("a certificate needs at least one hostname")
        if self.status not in CERTIFICATE_STATUSES:
            raise ValueError(
                f"certificate status must be one of {sorted(CERTIFICATE_STATUSES)}"
            )
        if self.is_wildcard and (
            len(self.hostnames) != 1 or not self.hostnames[0].startswith("*.")
        ):
            raise ValueError("a wildcard certificate has exactly one wildcard name")

    @property
    def is_live(self) -> bool:
        return self.status in {"pending", "issued"}

    def covers(self, hostname: str) -> bool:
        wanted = canonical_hostname(hostname)
        for name in self.hostnames:
            if name == wanted:
                return True
            if name.startswith("*."):
                suffix = name[1:]
                if wanted.endswith(suffix) and len(wanted) > len(suffix):
                    return True
        return False


@dataclass(frozen=True)
class IssuedCertificate:
    """What an ACME/cert-manager adapter returns after one successful issuance."""

    serial: str
    issued_at: datetime
    expires_at: datetime
    hostnames: tuple[str, ...] = ()


@dataclass(frozen=True)
class CertificateAlert:
    """A page-worthy certificate event (R-6)."""

    severity: str
    action: str
    kind: str
    message: str
    hostnames: tuple[str, ...] = ()
    threshold_days: int | None = None
    expires_at: datetime | None = None

    @property
    def should_page(self) -> bool:
        return self.action.startswith("page")


@dataclass(frozen=True)
class NotFoundResponse:
    """The designed 404.  It intentionally contains no platform or ingress identity."""

    status_code: int = 404
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Resolution:
    """The result of resolving one request against the route table."""

    action: str
    status_code: int
    hostname: str
    path: str
    route: RouteRule | None = None
    location: str | None = None
    message: str | None = None
    rewrite_depth: int = 0
    headers: Mapping[str, str] = field(default_factory=dict)
    body: str = ""

    def __post_init__(self) -> None:
        if self.action not in RESOLUTION_ACTIONS:
            raise ValueError(f"invalid resolution action {self.action!r}")


@dataclass(frozen=True)
class IngressSnapshot:
    """The complete input to the pure renderer (one poll's table read)."""

    routes: tuple[RouteRule, ...]
    domains: tuple[DomainRecord, ...] = ()
    certificates: tuple[CertificateRecord, ...] = ()

    def active_domain(self, hostname: str) -> DomainRecord | None:
        wanted = canonical_hostname(hostname)
        for domain in self.domains:
            if domain.hostname == wanted and domain.can_receive_traffic:
                return domain
        return None

    def domain_for(self, hostname: str) -> DomainRecord | None:
        wanted = canonical_hostname(hostname)
        for domain in self.domains:
            if domain.hostname == wanted:
                return domain
        return None


@dataclass(frozen=True)
class DomainOnboarding:
    """The two-hostname result of one onboarding transaction."""

    apex: str
    www: str
    apex_domain: DomainRecord
    www_domain: DomainRecord
    apex_route: RouteRule
    www_route: RouteRule
    created: bool
    dns_instructions: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconcileResult:
    """One reconciliation pass, suitable for logs and alerting."""

    applied: int
    deleted: int
    changed: int
    failed: bool
    error: str | None = None
    rendered: int = 0


@dataclass(frozen=True)
class OutboxMessage:
    id: str
    topic: str
    idempotency_key: str
    payload: Mapping[str, Any]
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None = None
    worker_id: str | None = None
    last_error: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", str(self.id))
        if self.status not in {"pending", "running", "succeeded", "dead"}:
            raise ValueError(f"invalid outbox status {self.status!r}")
        if self.attempts < 0 or self.max_attempts < 1:
            raise ValueError("invalid outbox attempt counters")


def route_from_row(row: Mapping[str, Any]) -> RouteRule:
    return RouteRule(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        project_id=str(row["project_id"]),
        hostname=str(row["hostname"]),
        path_prefix=str(row.get("path_prefix", "/")),
        kind=str(row.get("kind", "proxy")),
        target_path=row.get("target_path"),
        status_code=int(row.get("status_code", 200)),
        priority=int(row.get("priority", 0)),
        deployment_id=str(row["deployment_id"]) if row.get("deployment_id") else None,
        revision=row.get("revision"),
        namespace=row.get("namespace"),
        service_name=row.get("service_name"),
        service_port=row.get("service_port"),
    )


def domain_from_row(row: Mapping[str, Any]) -> DomainRecord:
    return DomainRecord(
        id=str(row["id"]),
        org_id=str(row["org_id"]),
        project_id=str(row["project_id"]),
        hostname=str(row["hostname"]),
        status=str(row["status"]),
        is_production=bool(row.get("is_production", False)),
        verification_token=str(row.get("verification_token", "")),
        cert_issued_at=row.get("cert_issued_at"),
        cert_expires_at=row.get("cert_expires_at"),
        last_checked_at=row.get("last_checked_at"),
    )


def certificate_from_row(row: Mapping[str, Any]) -> CertificateRecord:
    return CertificateRecord(
        id=str(row["id"]),
        org_id=str(row["org_id"]) if row.get("org_id") else None,
        registered_domain=str(row["registered_domain"]),
        hostnames=tuple(str(h) for h in row["hostnames"]),
        is_wildcard=bool(row["is_wildcard"]),
        status=str(row["status"]),
        idempotency_key=str(row["idempotency_key"]),
        issuer=str(row["issuer"]),
        serial=row.get("serial"),
        issued_at=row.get("issued_at"),
        expires_at=row.get("expires_at"),
        revoked_at=row.get("revoked_at"),
        last_error=row.get("last_error"),
        created_at=row.get("created_at"),
    )


def new_id() -> str:
    return str(uuid.uuid4())


__all__ = [
    "CERTIFICATE_STATUSES",
    "DOMAIN_STATUSES",
    "ROUTE_KINDS",
    "CertificateAlert",
    "CertificateRecord",
    "DomainOnboarding",
    "DomainRecord",
    "IngressSnapshot",
    "IssuedCertificate",
    "NotFoundResponse",
    "OutboxMessage",
    "ReconcileResult",
    "Resolution",
    "RouteRule",
    "certificate_from_row",
    "domain_from_row",
    "new_id",
    "route_from_row",
]
