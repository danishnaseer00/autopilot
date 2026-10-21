"""SPEC-15 custom-domain onboarding, verification and suspension.

The API calls :class:`DomainService` inside its tenant transaction.  The service
does not commit: the two domain rows, the apex route and the www redirect are
one transaction (R-3).  A pending or suspended domain stays in ``routes`` but is
never rendered into an Ingress (AC-3/AC-4).

Side effects with an external dependency (certificate issuance/revocation and a
later DNS re-check) go through :class:`~runtime.ingress.outbox.OutboxStore`, not
through a best-effort API call in this transaction.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Mapping, Protocol

from runtime.ingress.certificates import CertificateStore
from runtime.ingress.errors import (
    DomainAlreadyClaimed,
    DomainNotFound,
    InvalidHostname,
    OutboxError,
)
from runtime.ingress.models import (
    DomainRecord,
    IssuedCertificate,
    OutboxMessage,
    RouteRule,
)
from runtime.ingress.outbox import OutboxStore
from runtime.ingress.validation import canonical_hostname

DOMAIN_RECHECK_TOPIC = "domain.recheck"
CERTIFICATE_ISSUE_TOPIC = "certificate.issue"
CERTIFICATE_REVOKE_TOPIC = "certificate.revoke"


class DomainVerifier(Protocol):
    """DNS/TXT check for one hostname (the real adapter is cluster-owned)."""

    async def verify(self, domain: DomainRecord) -> bool: ...


class DomainStore(Protocol):
    async def find(self, hostname: str) -> DomainRecord | None: ...

    async def create(self, domain: DomainRecord) -> DomainRecord: ...

    async def update_status(
        self,
        domain_id: str,
        status: str,
        *,
        now: datetime,
    ) -> DomainRecord: ...

    async def activate(
        self,
        domain_id: str,
        *,
        issued: IssuedCertificate,
    ) -> DomainRecord: ...

    async def list_all(self) -> tuple[DomainRecord, ...]: ...

    async def delete(self, hostname: str) -> None: ...

    async def upsert_route(self, route: RouteRule) -> RouteRule: ...

    async def route_for(self, hostname: str) -> RouteRule | None: ...

    async def delete_routes(self, hostname: str) -> int: ...


@dataclass(frozen=True)
class OnboardingPlan:
    apex: str
    www: str
    redirect_direction: str
    apex_route: RouteRule
    www_route: RouteRule
    verification_tokens: Mapping[str, str]
    dns_instructions: Mapping[str, str]


@dataclass(frozen=True)
class VerificationResult:
    domain: DomainRecord
    verified: bool
    suspended: bool
    requires_certificate: bool


@dataclass(frozen=True)
class RemovalResult:
    hostname: str
    revoked_certificate_ids: tuple[str, ...]
    outbox_message_ids: tuple[str, ...]


def _www(apex: str) -> str:
    return "www." + apex


def _dns_instructions(
    *,
    apex: str,
    www: str,
    tokens: Mapping[str, str],
    edge_target: str,
) -> dict[str, str]:
    instructions: dict[str, str] = {}
    for host in (apex, www):
        instructions[host] = f"CNAME {edge_target}"
        instructions[f"_autopilot-challenge.{host}"] = f"TXT {tokens[host]}"
    return instructions


def plan_onboarding(
    *,
    hostname: str,
    org_id: str,
    project_id: str,
    namespace: str,
    apex: str | None = None,
    redirect_direction: str = "www_to_apex",
    edge_target: str = "edge.autopilot.app",
    tokens: Mapping[str, str] | None = None,
    id_factory: Callable[[], str] | None = None,
) -> OnboardingPlan:
    """Build the two domain rows and two routes without touching a database.

    ``hostname`` may be the apex or its ``www`` form.  The canonical apex is the
    one recorded as the certificate budget key.  The default redirect allows the
    verifier to prove both names before either one serves application traffic.
    """

    if redirect_direction not in {"www_to_apex", "apex_to_www"}:
        raise ValueError("redirect_direction must be www_to_apex or apex_to_www")
    candidate = canonical_hostname(hostname)
    apex = canonical_hostname(apex) if apex else (
        candidate[4:] if candidate.startswith("www.") else candidate
    )
    if apex.startswith("www."):
        raise InvalidHostname("apex must not start with 'www.'")
    if candidate not in {apex, _www(apex)}:
        raise InvalidHostname(f"hostname {candidate!r} is not {apex!r} or its www host")
    www = _www(apex)

    if tokens is None:
        tokens = {
            apex: secrets.token_urlsafe(24),
            www: secrets.token_urlsafe(24),
        }
    else:
        missing = [host for host in (apex, www) if host not in tokens]
        if missing:
            raise ValueError(
                "tokens must contain both the apex and www host: "
                + ", ".join(missing)
            )
        tokens = {apex: tokens[apex], www: tokens[www]}
        if any(not isinstance(value, str) or not value for value in tokens.values()):
            raise ValueError("verification tokens must be non-empty strings")

    new_id = id_factory or (lambda: secrets.token_hex(16))
    if redirect_direction == "www_to_apex":
        proxy_host, redirect_host = apex, www
        redirect_target = f"https://{apex}"
    else:
        proxy_host, redirect_host = www, apex
        redirect_target = f"https://{www}"

    proxy_route = RouteRule(
        id=new_id(),
        org_id=org_id,
        project_id=project_id,
        hostname=proxy_host,
        path_prefix="/",
        kind="proxy",
        target_path=None,
        status_code=200,
        namespace=namespace,
    )
    redirect_route = RouteRule(
        id=new_id(),
        org_id=org_id,
        project_id=project_id,
        hostname=redirect_host,
        path_prefix="/",
        kind="redirect",
        target_path=redirect_target,
        status_code=301,
        namespace=namespace,
    )
    apex_route = proxy_route if proxy_host == apex else redirect_route
    www_route = proxy_route if proxy_host == www else redirect_route
    return OnboardingPlan(
        apex=apex,
        www=www,
        redirect_direction=redirect_direction,
        apex_route=apex_route,
        www_route=www_route,
        verification_tokens=dict(tokens),
        dns_instructions=_dns_instructions(
            apex=apex,
            www=www,
            tokens=tokens,
            edge_target=edge_target,
        ),
    )


class InMemoryDomainStore:
    """Reference store for AC-3/AC-4/AC-5 and route-ownership tests."""

    def __init__(self) -> None:
        self._domains: dict[str, DomainRecord] = {}
        self._routes: dict[tuple[str, str], RouteRule] = {}
        self._lock = asyncio.Lock()

    async def find(self, hostname: str) -> DomainRecord | None:
        return self._domains.get(canonical_hostname(hostname))

    async def create(self, domain: DomainRecord) -> DomainRecord:
        async with self._lock:
            existing = self._domains.get(domain.hostname)
            if existing is not None:
                if (
                    existing.org_id != domain.org_id
                    or existing.project_id != domain.project_id
                ):
                    raise DomainAlreadyClaimed(
                        f"hostname {domain.hostname!r} is already owned by another project"
                    )
                return existing
            self._domains[domain.hostname] = domain
            return domain

    async def update_status(
        self, domain_id: str, status: str, *, now: datetime
    ) -> DomainRecord:
        async with self._lock:
            for hostname, current in self._domains.items():
                if current.id != str(domain_id):
                    continue
                updated = DomainRecord(
                    **{
                        **current.__dict__,
                        "status": status,
                        "last_checked_at": now,
                    }
                )
                self._domains[hostname] = updated
                return updated
        raise DomainNotFound(str(domain_id))

    async def activate(
        self, domain_id: str, *, issued: IssuedCertificate
    ) -> DomainRecord:
        async with self._lock:
            for hostname, current in self._domains.items():
                if current.id != str(domain_id):
                    continue
                updated = DomainRecord(
                    **{
                        **current.__dict__,
                        "status": "active",
                        "cert_issued_at": issued.issued_at,
                        "cert_expires_at": issued.expires_at,
                    }
                )
                self._domains[hostname] = updated
                return updated
        raise DomainNotFound(str(domain_id))

    async def list_all(self) -> tuple[DomainRecord, ...]:
        return tuple(
            sorted(self._domains.values(), key=lambda item: item.hostname)
        )

    async def delete(self, hostname: str) -> None:
        async with self._lock:
            if canonical_hostname(hostname) not in self._domains:
                raise DomainNotFound(hostname)
            del self._domains[canonical_hostname(hostname)]

    async def upsert_route(self, route: RouteRule) -> RouteRule:
        async with self._lock:
            key = (route.hostname, route.path_prefix)
            existing = self._routes.get(key)
            if existing is not None and existing.project_id != route.project_id:
                raise DomainAlreadyClaimed(
                    f"route {route.hostname}{route.path_prefix} is owned by another project"
                )
            self._routes[key] = route
            return route

    async def route_for(self, hostname: str) -> RouteRule | None:
        return self._routes.get((canonical_hostname(hostname), "/"))

    async def delete_routes(self, hostname: str) -> int:
        async with self._lock:
            host = canonical_hostname(hostname)
            keys = [key for key in self._routes if key[0] == host]
            for key in keys:
                del self._routes[key]
            return len(keys)


class DomainService:
    """Onboard, verify, suspend, reactivate and remove a custom domain."""

    def __init__(
        self,
        store: DomainStore,
        *,
        outbox: OutboxStore | None = None,
        certificate_store: CertificateStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._outbox = outbox
        self._certificate_store = certificate_store
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def onboard(
        self,
        *,
        hostname: str,
        org_id: str,
        project_id: str,
        namespace: str,
        apex: str | None = None,
        redirect_direction: str = "www_to_apex",
        edge_target: str = "edge.autopilot.app",
        tokens: Mapping[str, str] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> tuple[OnboardingPlan, tuple[DomainRecord, ...], bool]:
        plan = plan_onboarding(
            hostname=hostname,
            org_id=org_id,
            project_id=project_id,
            namespace=namespace,
            apex=apex,
            redirect_direction=redirect_direction,
            edge_target=edge_target,
            tokens=tokens,
            id_factory=id_factory,
        )
        created = False
        domains: list[DomainRecord] = []
        now = self._clock()

        for host, token in (
            (plan.apex, plan.verification_tokens[plan.apex]),
            (plan.www, plan.verification_tokens[plan.www]),
        ):
            existing = await self._store.find(host)
            if existing is not None:
                if (
                    existing.org_id != str(org_id)
                    or existing.project_id != str(project_id)
                ):
                    raise DomainAlreadyClaimed(
                        f"hostname {host!r} is already owned by another project"
                    )
                domains.append(existing)
                continue
            domain = DomainRecord(
                id=str(uuid.uuid4()),
                org_id=str(org_id),
                project_id=str(project_id),
                hostname=host,
                status="pending",
                is_production=True,
                verification_token=token,
                last_checked_at=now,
            )
            domains.append(await self._store.create(domain))
            created = True

        # R-3: both routes are written in the same transaction.  The renderer
        # filters on domain status, so a pending route cannot receive traffic.
        await self._store.upsert_route(plan.apex_route)
        await self._store.upsert_route(plan.www_route)
        return plan, tuple(domains), created

    async def recheck(
        self, hostname: str, verifier: DomainVerifier
    ) -> VerificationResult:
        """One verification cycle for one hostname (R-4/AC-4)."""

        domain = await self._store.find(hostname)
        if domain is None:
            raise DomainNotFound(hostname)
        now = self._clock()
        verified = await verifier.verify(domain)

        if not verified:
            # A missing CNAME/TXT record suspends a live domain, not just fails
            # the initial check.  The route rows remain for audit but are not
            # rendered while status != active.
            next_status = "suspended" if domain.status == "active" else "failed"
            updated = await self._store.update_status(domain.id, next_status, now=now)
            return VerificationResult(
                domain=updated,
                verified=False,
                suspended=next_status == "suspended",
                requires_certificate=False,
            )

        if domain.cert_issued_at is not None:
            next_status = "active"
            requires_certificate = False
        else:
            next_status = "verified"
            requires_certificate = True
        updated = await self._store.update_status(domain.id, next_status, now=now)
        return VerificationResult(
            domain=updated,
            verified=True,
            suspended=False,
            requires_certificate=requires_certificate,
        )

    async def recheck_all(self, verifier: DomainVerifier) -> tuple[VerificationResult, ...]:
        results: list[VerificationResult] = []
        for domain in await self._store.list_all():
            results.append(await self.recheck(domain.hostname, verifier))
        return tuple(results)

    async def activate(self, hostname: str, issued: IssuedCertificate) -> DomainRecord:
        domain = await self._store.find(hostname)
        if domain is None:
            raise DomainNotFound(hostname)
        return await self._store.activate(domain.id, issued=issued)

    async def request_recheck(
        self,
        hostname: str,
        *,
        idempotency_key: str | None = None,
    ) -> OutboxMessage:
        """Durably ask a worker to re-check one domain after a DNS change (R-4).

        The periodic sweep can call :meth:`recheck_all` directly.  An immediate
        check triggered by an external DNS notification must survive a pod
        restart, so it goes through the outbox instead of an in-process task.
        """

        if self._outbox is None:
            raise OutboxError("domain re-check requires a durable outbox")
        domain = await self._store.find(hostname)
        if domain is None:
            raise DomainNotFound(hostname)
        now = self._clock()
        key = idempotency_key or f"recheck:{domain.hostname}:{now.isoformat()}"
        return await self._outbox.enqueue(
            DOMAIN_RECHECK_TOPIC,
            key,
            {"hostname": domain.hostname},
            now=now,
        )

    async def remove(self, hostname: str) -> RemovalResult:
        """Remove the domain and enqueue immediate certificate revocation (R-7).

        The row is removed in the caller's transaction; the outbox message is
        committed with it, so a crash after commit still revokes the certificate.
        The API does not wait for the certificate's expiry date.
        """

        if self._outbox is None or self._certificate_store is None:
            raise OutboxError(
                "domain removal requires a durable outbox and certificate ledger"
            )
        domain = await self._store.find(hostname)
        if domain is None:
            raise DomainNotFound(hostname)

        certificate_ids: list[str] = []
        message_ids: list[str] = []
        now = self._clock()
        for certificate in await self._certificate_store.live():
            if not certificate.covers(domain.hostname):
                continue
            certificate_ids.append(certificate.id)
            message = await self._outbox.enqueue(
                CERTIFICATE_REVOKE_TOPIC,
                f"revoke:{certificate.id}",
                {"certificate_id": certificate.id, "hostname": domain.hostname},
                now=now,
            )
            message_ids.append(message.id)

        deleted_routes = await self._store.delete_routes(domain.hostname)
        if deleted_routes < 0:  # pragma: no cover - defensive
            raise OutboxError("invalid route deletion count")
        await self._store.delete(domain.hostname)
        return RemovalResult(
            hostname=domain.hostname,
            revoked_certificate_ids=tuple(certificate_ids),
            outbox_message_ids=tuple(message_ids),
        )


__all__ = [
    "CERTIFICATE_ISSUE_TOPIC",
    "CERTIFICATE_REVOKE_TOPIC",
    "DOMAIN_RECHECK_TOPIC",
    "DomainService",
    "DomainStore",
    "DomainVerifier",
    "InMemoryDomainStore",
    "OnboardingPlan",
    "RemovalResult",
    "VerificationResult",
    "plan_onboarding",
]
