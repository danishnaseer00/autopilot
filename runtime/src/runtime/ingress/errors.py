"""Error taxonomy for SPEC-15.

Messages never contain certificate private keys or challenge secrets.  Hosts,
registered domains, limits and state names are safe to log.
"""

from __future__ import annotations

from datetime import timedelta


class IngressError(Exception):
    """Base class for every SPEC-15 failure."""


class InvalidHostname(IngressError, ValueError):
    """A hostname is not a valid DNS name or wildcard pattern."""


class InvalidRoute(IngressError, ValueError):
    """A route row cannot be rendered or resolved as declared."""


class IngressRenderError(IngressError):
    """The full desired state could not be computed; nothing was applied."""


class RewriteDepthExceeded(IngressError):
    """A rewrite chain did not terminate inside the bounded depth."""

    def __init__(self, *, depth: int, limit: int, hostname: str) -> None:
        super().__init__(
            f"rewrite depth limit {limit} exceeded for host {hostname!r} "
            f"(stopped at depth {depth}); the route set contains a cycle"
        )
        self.depth = depth
        self.limit = limit
        self.hostname = hostname


class CertificateError(IngressError):
    """Base class for certificate lifecycle failures."""


class CertificateBudgetExceeded(CertificateError):
    """R-2: a registered domain's weekly issuance budget is exhausted."""

    def __init__(
        self,
        registered_domain: str,
        *,
        limit: int,
        window: timedelta,
        used: int,
    ) -> None:
        days = window.days if window.seconds == 0 else window.total_seconds() / 86400
        super().__init__(
            f"certificate budget exceeded for {registered_domain!r}: "
            f"{used} already requested in the last {days:g} days "
            f"(limit {limit}); refusing to consume more of the weekly quota"
        )
        self.registered_domain = registered_domain
        self.limit = limit
        self.used = used
        self.window = window


class CertificateNotFound(CertificateError):
    """No certificate row matches the id."""


class CertificateIssueFailed(CertificateError):
    """The ACME backend did not issue or renew the certificate."""


class CertificateRevokeFailed(CertificateError):
    """The ACME backend did not revoke the certificate."""


class DomainError(IngressError):
    """Base class for custom-domain failures."""


class DomainAlreadyClaimed(DomainError):
    """The hostname is owned by another org/project; R-3's takeover guard."""


class DomainNotFound(DomainError):
    """No domain row matches the id within the caller's tenant."""


class InvalidDomainState(DomainError):
    """The requested domain transition is not allowed."""


class UnverifiedDomain(DomainError):
    """A domain is not active, so it must not receive ingress traffic."""


class OutboxError(IngressError):
    """The durable queue could not accept or process work."""


class OutboxMessageNotFound(OutboxError):
    """No outbox row matches the id."""


__all__ = [
    "CertificateBudgetExceeded",
    "CertificateError",
    "CertificateIssueFailed",
    "CertificateNotFound",
    "CertificateRevokeFailed",
    "DomainAlreadyClaimed",
    "DomainError",
    "DomainNotFound",
    "IngressError",
    "IngressRenderError",
    "InvalidDomainState",
    "InvalidHostname",
    "InvalidRoute",
    "OutboxError",
    "OutboxMessageNotFound",
    "RewriteDepthExceeded",
    "UnverifiedDomain",
]
