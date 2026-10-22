"""Edge rate limiting and trusted-proxy attribution (SPEC-16 R-17/R-18).

The limiter is a fixed window keyed by hostname and resolved client IP.  Redis
is the production store, but it is deliberately behind :class:`RateLimitStore`:
the data plane must still serve when Redis is down (AC-7), so every store error
or timeout is a fail-open decision.  The in-memory store here is the reference
implementation used by local tests; cluster wiring is coordinator-owned.

``X-Forwarded-For`` is used only when the direct peer is in ``trusted_proxies``.
The chain is walked from the right, skipping trusted proxies, because a client
can prepend arbitrary addresses to the left of the header.  A malformed chain
is ignored rather than trusted.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _parse_ip(value: str) -> IPAddress | None:
    """Parse an address, tolerating the common ``host:port`` peer forms."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("["):
        end = candidate.find("]")
        if end == -1:
            return None
        candidate = candidate[1:end]
    elif candidate.count(":") == 1:
        host, port = candidate.rsplit(":", 1)
        if port.isdigit():
            candidate = host
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        return None


def _forwarded_chain(
    forwarded_for: str | Sequence[str] | None,
) -> tuple[IPAddress, ...] | None:
    """Parse a complete X-Forwarded-For chain, or return ``None`` if malformed."""

    if forwarded_for is None:
        return ()
    values = [forwarded_for] if isinstance(forwarded_for, str) else list(forwarded_for)
    entries: list[str] = []
    for value in values:
        if not isinstance(value, str):
            return None
        entries.extend(part.strip() for part in value.split(","))

    if not entries or any(not entry for entry in entries):
        return None

    parsed: list[IPAddress] = []
    for entry in entries:
        ip = _parse_ip(entry)
        if ip is None:
            return None
        parsed.append(ip)
    return tuple(parsed)


class ProxyTrust:
    """Resolve the client address through a configured trusted-proxy boundary."""

    __slots__ = ("_networks",)

    def __init__(
        self,
        trusted_proxies: Iterable[str | IPNetwork] = (),
    ) -> None:
        networks: list[IPNetwork] = []
        for value in trusted_proxies:
            try:
                if isinstance(value, (ipaddress.IPv4Network, ipaddress.IPv6Network)):
                    network = value
                else:
                    network = ipaddress.ip_network(str(value), strict=False)
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy {value!r}") from exc
            networks.append(network)
        self._networks = tuple(networks)

    def _is_trusted(self, address: IPAddress) -> bool:
        for network in self._networks:
            if address.version != network.version:
                continue
            if address in network:
                return True
        return False

    def client_ip(
        self,
        remote_addr: str | None,
        forwarded_for: str | Sequence[str] | None = None,
    ) -> str:
        """Return the attributable client address.

        Direct peers outside the trust boundary are always the client.  For a
        trusted peer, the right-most non-trusted entry in the forwarded chain is
        the client; a spoofed entry on the left never wins.
        """

        remote = _parse_ip(remote_addr or "")
        if remote is None:
            return "unknown"

        if not self._networks or not self._is_trusted(remote):
            return str(remote)

        chain = _forwarded_chain(forwarded_for)
        if chain is None or not chain:
            return str(remote)

        for candidate in reversed(chain):
            if not self._is_trusted(candidate):
                return str(candidate)
        # Every hop is trusted.  The left-most entry is the original peer; fall
        # back to the socket peer if the header was empty.
        return str(chain[0])

    @property
    def networks(self) -> tuple[IPNetwork, ...]:
        return self._networks


@dataclass(frozen=True)
class RateLimitPolicy:
    limit: int = 600
    window_seconds: int = 60

    def __post_init__(self) -> None:
        if isinstance(self.limit, bool) or not isinstance(self.limit, int):
            raise TypeError("rate limit must be an integer")
        if self.limit < 1:
            raise ValueError("rate limit must be positive")
        if isinstance(self.window_seconds, bool) or not isinstance(
            self.window_seconds, int
        ):
            raise TypeError("rate-limit window must be an integer")
        if self.window_seconds < 1:
            raise ValueError("rate-limit window must be positive")


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0
    degraded: bool = False

    def headers(self) -> dict[str, str]:
        headers = {
            "x-ratelimit-limit": str(self.limit),
            "x-ratelimit-remaining": str(max(self.remaining, 0)),
        }
        if not self.allowed:
            headers["retry-after"] = str(max(self.retry_after, 1))
        return headers


class RateLimitStore(Protocol):
    """Atomic fixed-window counter.

    ``incr`` returns ``(count, ttl_seconds)`` for the current window.  A Redis
    implementation should use INCR plus an expiry (or an equivalent Lua script)
    and return the remaining TTL.  The reconciler never calls this interface.
    """

    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]: ...


class InMemoryRateLimitStore:
    """Reference store with explicit time injection for deterministic tests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._windows: dict[str, tuple[int, datetime]] = {}
        self._lock = asyncio.Lock()

    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        if not key:
            raise ValueError("rate-limit key must not be empty")
        if ttl_seconds < 1:
            raise ValueError("rate-limit ttl must be positive")

        async with self._lock:
            now = self._clock()
            count, expires_at = self._windows.get(key, (0, now))
            if count == 0 or now >= expires_at:
                count = 1
                expires_at = now + timedelta(seconds=ttl_seconds)
            else:
                count += 1
            self._windows[key] = (count, expires_at)
            remaining = math.ceil((expires_at - now).total_seconds())
            return count, max(1, min(ttl_seconds, remaining))

    def clear(self) -> None:
        self._windows.clear()


class RateLimiter:
    """Fixed-window limiter with a hard fail-open timeout."""

    def __init__(
        self,
        store: RateLimitStore,
        policy: RateLimitPolicy | None = None,
        *,
        timeout_seconds: float = 0.05,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("rate-limit timeout must be positive")
        self._store = store
        self._policy = policy or RateLimitPolicy()
        self._timeout_seconds = timeout_seconds

    @property
    def policy(self) -> RateLimitPolicy:
        return self._policy

    async def check(
        self,
        hostname: str,
        client_ip: str,
        *,
        policy: RateLimitPolicy | None = None,
    ) -> RateLimitDecision:
        """Count one request, optionally using the installed config's policy."""

        active = policy or self._policy
        key = f"rl:{hostname}:{client_ip}"
        try:
            count, ttl = await asyncio.wait_for(
                self._store.incr(key, active.window_seconds),
                timeout=self._timeout_seconds,
            )
            if not isinstance(count, int) or count < 1:
                raise ValueError("rate-limit store returned an invalid count")
            if not isinstance(ttl, int):
                ttl = int(ttl)
        except Exception:  # noqa: BLE001 - store outages must fail open
            return RateLimitDecision(
                allowed=True,
                limit=active.limit,
                remaining=active.limit,
                degraded=True,
            )

        allowed = count <= active.limit
        return RateLimitDecision(
            allowed=allowed,
            limit=active.limit,
            remaining=max(active.limit - count, 0),
            retry_after=max(1, ttl) if not allowed else 0,
        )


__all__ = [
    "InMemoryRateLimitStore",
    "ProxyTrust",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimitStore",
    "RateLimiter",
]
