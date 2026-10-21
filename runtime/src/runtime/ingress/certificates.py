"""SPEC-15 certificate lifecycle: one wildcard, a weekly budget, alerts, revoke.

The certificate store is the durable ledger.  The in-memory implementation is
the reference used by local acceptance tests; ``runtime.ingress.postgres``
implements the same protocol against ``ingress_certificates`` and
``usage_daily``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol, Sequence

from runtime.ingress.errors import (
    CertificateBudgetExceeded,
    CertificateIssueFailed,
    CertificateNotFound,
    CertificateRevokeFailed,
)
from runtime.ingress.models import (
    CertificateAlert,
    CertificateRecord,
    IssuedCertificate,
)
from runtime.ingress.validation import (
    canonical_hostname,
    registered_domain,
    wildcard_certificate_name,
)

CERTIFICATE_WEEK = timedelta(days=7)
DEFAULT_WEEKLY_LIMIT = 5


class CertificateIssuer(Protocol):
    """The cert-manager/ACME side effect, kept behind a narrow seam."""

    @property
    def name(self) -> str: ...

    async def issue(
        self, *, hostnames: Sequence[str], registered_domain: str
    ) -> IssuedCertificate: ...

    async def revoke(self, *, hostnames: Sequence[str], serial: str | None) -> None: ...


class CertificateStore(Protocol):
    async def get(self, certificate_id: str) -> CertificateRecord | None: ...

    async def active_for(
        self,
        *,
        registered_domain: str,
        hostnames: Sequence[str],
        is_wildcard: bool,
    ) -> CertificateRecord | None: ...

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
    ) -> CertificateRecord: ...

    async def mark_issued(
        self, certificate_id: str, issued: IssuedCertificate
    ) -> CertificateRecord: ...

    async def mark_failed(
        self, certificate_id: str, error: str, now: datetime
    ) -> CertificateRecord: ...

    async def revoke(self, certificate_id: str, now: datetime) -> CertificateRecord: ...

    async def live(self) -> tuple[CertificateRecord, ...]: ...


@dataclass(frozen=True)
class CertificatePolicy:
    weekly_limit: int = DEFAULT_WEEKLY_LIMIT
    window: timedelta = CERTIFICATE_WEEK
    renewal_alert_days: tuple[int, ...] = (30, 14, 7)

    def __post_init__(self) -> None:
        if (
            isinstance(self.weekly_limit, bool)
            or not isinstance(self.weekly_limit, int)
            or self.weekly_limit < 1
        ):
            raise ValueError("weekly_limit must be a positive integer")
        if self.window <= timedelta(0):
            raise ValueError("window must be positive")
        if tuple(sorted(self.renewal_alert_days)) != (7, 14, 30):
            raise ValueError("renewal_alert_days must be exactly 7, 14 and 30")


class InMemoryCertificateStore:
    """Reference store with the same budgeting rules as the SQL adapter."""

    def __init__(self) -> None:
        self._rows: dict[str, CertificateRecord] = {}
        self._lock = asyncio.Lock()

    async def get(self, certificate_id: str) -> CertificateRecord | None:
        return self._rows.get(str(certificate_id))

    async def active_for(
        self,
        *,
        registered_domain: str,
        hostnames: Sequence[str],
        is_wildcard: bool,
    ) -> CertificateRecord | None:
        wanted = tuple(
            canonical_hostname(host) if not host.startswith("*.") else host
            for host in hostnames
        )
        for row in sorted(
            self._rows.values(),
            key=lambda item: item.created_at
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        ):
            if row.registered_domain != registered_domain:
                continue
            if row.is_wildcard != is_wildcard or row.status not in {"pending", "issued"}:
                continue
            if set(row.hostnames) >= set(wanted):
                return row
        return None

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
        async with self._lock:
            for row in self._rows.values():
                if row.idempotency_key == idempotency_key:
                    return row
            if is_wildcard:
                for row in self._rows.values():
                    if (
                        row.registered_domain == registered_domain
                        and row.is_wildcard
                        and row.status in {"pending", "issued"}
                    ):
                        return row

            cutoff = now - window
            used = sum(
                1
                for row in self._rows.values()
                if row.registered_domain == registered_domain
                and row.created_at is not None
                and row.created_at >= cutoff
            )
            if used >= limit:
                raise CertificateBudgetExceeded(
                    registered_domain, limit=limit, window=window, used=used
                )

            normalised = tuple(
                wildcard_certificate_name(h[2:])
                if h.startswith("*.")
                else canonical_hostname(h)
                for h in hostnames
            )
            row = CertificateRecord(
                id=str(uuid.uuid4()),
                org_id=org_id,
                registered_domain=registered_domain,
                hostnames=normalised,
                is_wildcard=is_wildcard,
                status="pending",
                idempotency_key=idempotency_key,
                issuer=issuer,
                created_at=now,
            )
            self._rows[row.id] = row
            return row

    async def mark_issued(
        self, certificate_id: str, issued: IssuedCertificate
    ) -> CertificateRecord:
        async with self._lock:
            current = self._rows.get(str(certificate_id))
            if current is None:
                raise CertificateNotFound(str(certificate_id))
            updated = CertificateRecord(
                **{
                    **current.__dict__,
                    "status": "issued",
                    "serial": issued.serial,
                    "issued_at": issued.issued_at,
                    "expires_at": issued.expires_at,
                    "last_error": None,
                }
            )
            self._rows[updated.id] = updated
            return updated

    async def mark_failed(
        self, certificate_id: str, error: str, now: datetime
    ) -> CertificateRecord:
        async with self._lock:
            current = self._rows.get(str(certificate_id))
            if current is None:
                raise CertificateNotFound(str(certificate_id))
            updated = CertificateRecord(
                **{**current.__dict__, "status": "failed", "last_error": error[:500]}
            )
            self._rows[updated.id] = updated
            return updated

    async def revoke(self, certificate_id: str, now: datetime) -> CertificateRecord:
        async with self._lock:
            current = self._rows.get(str(certificate_id))
            if current is None:
                raise CertificateNotFound(str(certificate_id))
            updated = CertificateRecord(
                **{**current.__dict__, "status": "revoked", "revoked_at": now}
            )
            self._rows[updated.id] = updated
            return updated

    async def live(self) -> tuple[CertificateRecord, ...]:
        return tuple(
            sorted(
                (row for row in self._rows.values() if row.status in {"pending", "issued"}),
                key=lambda row: row.id,
            )
        )


class CertificateService:
    """Issuance, reuse and revocation policy (R-1, R-2, R-7)."""

    def __init__(
        self,
        store: CertificateStore,
        issuer: CertificateIssuer,
        *,
        policy: CertificatePolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._issuer = issuer
        self._policy = policy or CertificatePolicy()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def policy(self) -> CertificatePolicy:
        return self._policy

    async def ensure_wildcard(
        self, preview_domain: str, *, org_id: str | None = None
    ) -> CertificateRecord:
        """R-1/AC-1: reuse one certificate forever; never issue per preview."""

        host = wildcard_certificate_name(preview_domain)
        budget_domain = registered_domain(preview_domain)
        existing = await self._store.active_for(
            registered_domain=budget_domain,
            hostnames=(host,),
            is_wildcard=True,
        )
        if existing is not None and existing.status == "issued":
            return existing

        now = self._clock()
        record = await self._store.reserve(
            org_id=org_id,
            registered_domain=budget_domain,
            hostnames=(host,),
            is_wildcard=True,
            idempotency_key=f"wildcard:{preview_domain}",
            issuer=self._issuer.name,
            now=now,
            window=self._policy.window,
            limit=self._policy.weekly_limit,
        )
        return await self._issue(record)

    async def request_custom(
        self,
        hostnames: Sequence[str],
        *,
        registered_domain: str,
        org_id: str | None,
        idempotency_key: str | None = None,
    ) -> CertificateRecord:
        """Issue one SAN certificate for the verified hostnames of an apex."""

        if isinstance(hostnames, (str, bytes)):
            raise ValueError("hostnames must be a sequence")
        apex = canonical_hostname(registered_domain)
        normalised: list[str] = []
        for hostname in hostnames:
            if hostname.startswith("*."):
                raise ValueError("custom certificates are not wildcards")
            host = canonical_hostname(hostname)
            if host != apex and not host.endswith("." + apex):
                raise ValueError(f"hostname {hostname!r} is outside apex {apex!r}")
            if host not in normalised:
                normalised.append(host)
        if not normalised:
            raise ValueError("at least one hostname is required")

        existing = await self._store.active_for(
            registered_domain=apex,
            hostnames=tuple(normalised),
            is_wildcard=False,
        )
        if existing is not None and existing.status == "issued":
            return existing

        now = self._clock()
        digest = hashlib.sha256("\n".join(sorted(normalised)).encode()).hexdigest()[:12]
        key = idempotency_key or f"custom:{apex}:{digest}:{now.date().isoformat()}"
        record = await self._store.reserve(
            org_id=org_id,
            registered_domain=apex,
            hostnames=tuple(normalised),
            is_wildcard=False,
            idempotency_key=key,
            issuer=self._issuer.name,
            now=now,
            window=self._policy.window,
            limit=self._policy.weekly_limit,
        )
        return await self._issue(record)

    async def revoke(self, certificate_id: str) -> CertificateRecord:
        """R-7: remove the live certificate now, not at expiry."""

        record = await self._store.get(str(certificate_id))
        if record is None:
            raise CertificateNotFound(str(certificate_id))
        if record.status == "revoked":
            return record
        try:
            await self._issuer.revoke(
                hostnames=record.hostnames, serial=record.serial
            )
        except Exception as exc:  # the caller's outbox retries this operation
            raise CertificateRevokeFailed(
                f"could not revoke certificate {record.id}: {type(exc).__name__}"
            ) from exc
        return await self._store.revoke(record.id, self._clock())

    async def _issue(self, record: CertificateRecord) -> CertificateRecord:
        if record.status == "issued" and record.expires_at is not None:
            return record
        try:
            issued = await self._issuer.issue(
                hostnames=record.hostnames,
                registered_domain=record.registered_domain,
            )
        except Exception as exc:
            failed = await self._store.mark_failed(
                record.id, f"{type(exc).__name__}: {exc}", self._clock()
            )
            raise CertificateIssueFailed(
                f"certificate issuance failed for {record.registered_domain!r}; "
                "the durable outbox will retry"
            ) from exc
        return await self._store.mark_issued(record.id, issued)


def certificate_alerts(
    certificates: Iterable[CertificateRecord],
    now: datetime,
    *,
    known_hostnames: Iterable[str] | None = None,
) -> tuple[CertificateAlert, ...]:
    """R-6/AC-12: turn certificate state into page-worthy alerts.

    For an expiring certificate only the tightest crossed threshold is emitted.
    Eight days from expiry therefore yields one S1 page for the 14-day
    threshold, not three duplicate pages.
    """

    alerts: list[CertificateAlert] = []
    known = (
        {canonical_hostname(h) for h in known_hostnames}
        if known_hostnames is not None
        else None
    )

    for certificate in certificates:
        if certificate.status == "revoked":
            continue
        if certificate.status == "failed":
            alerts.append(
                CertificateAlert(
                    severity="S1",
                    action="page immediately",
                    kind="renewal_failure",
                    message=(
                        "certificate renewal failed for "
                        + ", ".join(certificate.hostnames)
                    ),
                    hostnames=certificate.hostnames,
                )
            )
            continue
        if certificate.expires_at is None:
            continue

        remaining = certificate.expires_at - now
        days = remaining.total_seconds() / 86400
        threshold = None
        for candidate in (7, 14, 30):
            if days <= candidate:
                threshold = candidate
                break
        if threshold is not None:
            action = "page, escalate" if threshold == 7 else "page"
            alerts.append(
                CertificateAlert(
                    severity="S1",
                    action=action,
                    kind="expiry",
                    message=(
                        f"certificate for {', '.join(certificate.hostnames)} "
                        f"expires in {max(days, 0):.1f} days "
                        f"({threshold}-day threshold)"
                    ),
                    hostnames=certificate.hostnames,
                    threshold_days=threshold,
                    expires_at=certificate.expires_at,
                )
            )

        if known is not None:
            unexpected = [
                hostname
                for hostname in certificate.hostnames
                if not hostname.startswith("*.") and hostname not in known
            ]
            if unexpected:
                alerts.append(
                    CertificateAlert(
                        severity="S2",
                        action="page - possible takeover attempt",
                        kind="unexpected_issuance",
                        message=(
                            "certificate exists for known-platform-invalid host(s): "
                            + ", ".join(unexpected)
                        ),
                        hostnames=tuple(unexpected),
                        expires_at=certificate.expires_at,
                    )
                )

    return tuple(
        sorted(
            alerts,
            key=lambda alert: (
                alert.severity,
                alert.kind,
                alert.hostnames,
                alert.threshold_days or 0,
            ),
        )
    )


class FakeCertificateIssuer:
    """Deterministic issuer used by the local acceptance suite."""

    def __init__(
        self,
        *,
        validity: timedelta = timedelta(days=90),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = "fake-acme"
        self._validity = validity
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.calls: list[tuple[tuple[str, ...], str]] = []
        self.revoked: list[tuple[tuple[str, ...], str | None]] = []
        self.failures_remaining = 0

    async def issue(
        self, *, hostnames: Sequence[str], registered_domain: str
    ) -> IssuedCertificate:
        self.calls.append((tuple(hostnames), registered_domain))
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("ACME unavailable")
        now = self._clock()
        return IssuedCertificate(
            serial=f"serial-{len(self.calls)}",
            issued_at=now,
            expires_at=now + self._validity,
            hostnames=tuple(hostnames),
        )

    async def revoke(self, *, hostnames: Sequence[str], serial: str | None) -> None:
        self.revoked.append((tuple(hostnames), serial))


__all__ = [
    "CERTIFICATE_WEEK",
    "CertificateIssuer",
    "CertificatePolicy",
    "CertificateService",
    "CertificateStore",
    "DEFAULT_WEEKLY_LIMIT",
    "FakeCertificateIssuer",
    "InMemoryCertificateStore",
    "certificate_alerts",
]
