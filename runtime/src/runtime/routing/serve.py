"""The data-plane serving facade (SPEC-16 R-13, R-17, R-18, AC-7/AC-12).

``DataPlane`` holds one immutable routing config and performs no database, API
or control-plane calls.  The only optional dependency is the edge rate-limit
store, and a store failure or timeout is explicitly fail-open.  Resolution and
404 generation are entirely in memory.
"""

from __future__ import annotations

from collections.abc import Sequence

from runtime.ingress.models import NotFoundResponse, Resolution
from runtime.ingress.validation import canonical_hostname
from runtime.routing.config import RoutingConfig
from runtime.routing.ratelimit import ProxyTrust, RateLimiter, RateLimitPolicy


class DataPlane:
    """Serve requests from an installed config without touching platform state."""

    __slots__ = ("_config", "_proxy_trust", "_rate_limiter")

    def __init__(
        self,
        config: RoutingConfig,
        *,
        rate_limiter: RateLimiter | None = None,
        proxy_trust: ProxyTrust | None = None,
    ) -> None:
        self._config = config
        self._rate_limiter = rate_limiter
        self._proxy_trust = proxy_trust or ProxyTrust()

    @property
    def config(self) -> RoutingConfig:
        return self._config

    @property
    def rate_limit_policy(self) -> RateLimitPolicy | None:
        return self._config.rate_limit if self._rate_limiter is not None else None

    def install(self, config: RoutingConfig) -> None:
        """Atomically swap to a newly reconciled config."""

        self._config = config

    async def handle(
        self,
        *,
        hostname: str,
        path: str,
        remote_addr: str | None,
        forwarded_for: str | Sequence[str] | None = None,
    ) -> Resolution:
        """Resolve one request, applying rate limits before routing."""

        config = self._config
        try:
            host = canonical_hostname(hostname)
        except (TypeError, ValueError):
            return self._not_found(str(hostname), path, config.not_found)

        client_ip = self._proxy_trust.client_ip(remote_addr, forwarded_for)
        if self._rate_limiter is not None:
            decision = await self._rate_limiter.check(
                host,
                client_ip,
                policy=config.rate_limit,
            )
            if not decision.allowed:
                headers = decision.headers()
                headers["content-type"] = "text/plain; charset=utf-8"
                body = (
                    "Too many requests. "
                    f"Retry after {max(decision.retry_after, 1)} seconds.\n"
                )
                return Resolution(
                    action="ratelimited",
                    status_code=429,
                    hostname=host,
                    path=path,
                    headers=headers,
                    body=body,
                    message="rate limit exceeded",
                )

        try:
            return config.table.resolve(
                host,
                path,
                max_rewrites=config.max_rewrites,
            )
        except (TypeError, ValueError):
            # A malformed path or hostname must not crash the data plane.  The
            # response is the same identity-free 404 as an unknown host.
            return self._not_found(host, path, config.not_found)

    @staticmethod
    def _not_found(
        hostname: str,
        path: str,
        not_found: NotFoundResponse,
    ) -> Resolution:
        return Resolution(
            action="notfound",
            status_code=not_found.status_code,
            hostname=hostname,
            path=path,
            headers=not_found.headers,
            body=not_found.body,
        )


__all__ = ["DataPlane"]
