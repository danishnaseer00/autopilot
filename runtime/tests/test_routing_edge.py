"""SPEC-16 AC-7..AC-9 and AC-12: serving, attribution, 429 and 404."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from runtime.ingress.models import IngressSnapshot, RouteRule
from runtime.ingress.render import IngressConfig
from runtime.routing import (
    DataPlane,
    InMemoryRateLimitStore,
    ProxyTrust,
    RateLimiter,
    RateLimitPolicy,
    render_routing_config,
)

PREVIEW = "preview.example.com"
HOST = f"pr-1-repo-deadbeef.{PREVIEW}"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def route() -> RouteRule:
    return RouteRule(
        id="route-1",
        org_id="org-1",
        project_id="project-1",
        hostname=HOST,
        path_prefix="/",
        kind="proxy",
        namespace="autopilot-org",
        service_name="web",
        service_port=8080,
    )


def routing_config(policy: RateLimitPolicy | None = None):
    return render_routing_config(
        IngressSnapshot(routes=(route(),)),
        IngressConfig(preview_domain=PREVIEW),
        rate_limit=policy or RateLimitPolicy(),
    )


class DownStore:
    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        raise ConnectionError("redis is down")


class HangingStore:
    async def incr(self, key: str, ttl_seconds: int) -> tuple[int, int]:
        await asyncio.sleep(10)
        return 1, ttl_seconds


def test_ac7_serving_succeeds_when_redis_is_down_or_hanging():
    config = routing_config()
    for store in (DownStore(), HangingStore()):
        plane = DataPlane(
            config,
            rate_limiter=RateLimiter(
                store,
                RateLimitPolicy(limit=2, window_seconds=60),
                timeout_seconds=0.01,
            ),
        )
        result = asyncio.run(
            plane.handle(
                hostname=HOST,
                path="/",
                remote_addr="203.0.113.9",
            )
        )
        assert result.action == "proxy"
        assert result.status_code == 200
        assert result.route is not None
        assert result.route.id == "route-1"


def test_ac8_forwarded_for_is_only_trusted_from_a_configured_proxy():
    trust = ProxyTrust(["10.0.0.0/8", "2001:db8::/32"])

    # A direct client cannot evade attribution by adding its own header.
    assert trust.client_ip("203.0.113.9", "1.2.3.4") == "203.0.113.9"

    # The CDN appended the real client to the client-supplied spoofed value.
    # The right-most untrusted hop wins, not the left-most spoof.
    assert trust.client_ip("10.1.2.3", "1.2.3.4, 198.51.100.7") == "198.51.100.7"
    assert trust.client_ip("10.1.2.3", "198.51.100.7, 10.9.9.9") == "198.51.100.7"

    # Malformed chains are ignored rather than treated as client addresses.
    assert trust.client_ip("10.1.2.3", "not-an-ip") == "10.1.2.3"
    assert trust.client_ip("10.1.2.3", "") == "10.1.2.3"

    # No trust boundary means no forwarded-for parsing at all.
    assert ProxyTrust().client_ip("10.1.2.3", "198.51.100.7") == "10.1.2.3"

    # Peer addresses with ports and IPv6 are handled.
    assert trust.client_ip("10.1.2.3:443", "198.51.100.7") == "198.51.100.7"
    assert trust.client_ip("2001:db8::5", "198.51.100.7") == "198.51.100.7"


def test_ac9_429_has_retry_after_limit_and_remaining_headers():
    clock = [NOW]
    store = InMemoryRateLimitStore(clock=lambda: clock[0])
    policy = RateLimitPolicy(limit=2, window_seconds=60)
    plane = DataPlane(
        routing_config(policy),
        rate_limiter=RateLimiter(store, policy, timeout_seconds=0.1),
    )

    async def exercise():
        first = await plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
        second = await plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
        third = await plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
        return first, second, third

    first, second, third = asyncio.run(exercise())
    assert first.action == "proxy"
    assert second.action == "proxy"
    assert third.action == "ratelimited"
    assert third.status_code == 429
    assert third.headers["retry-after"] == "60"
    assert third.headers["x-ratelimit-limit"] == "2"
    assert third.headers["x-ratelimit-remaining"] == "0"
    assert third.headers["content-type"].startswith("text/plain")
    assert "Too many requests" in third.body

    # Fixed window: the next window admits the same client again.
    clock[0] = NOW + timedelta(seconds=61)
    fourth = asyncio.run(
        plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
    )
    assert fourth.action == "proxy"


def test_installed_config_policy_is_used_by_the_data_plane():
    store = InMemoryRateLimitStore(clock=lambda: NOW)
    config_policy = RateLimitPolicy(limit=1, window_seconds=60)
    plane = DataPlane(
        routing_config(config_policy),
        rate_limiter=RateLimiter(
            store,
            RateLimitPolicy(limit=999, window_seconds=60),
            timeout_seconds=0.1,
        ),
    )

    async def exercise():
        first = await plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
        second = await plane.handle(hostname=HOST, path="/", remote_addr="203.0.113.9")
        return first, second

    first, second = asyncio.run(exercise())
    assert plane.rate_limit_policy == config_policy
    assert first.action == "proxy"
    assert second.action == "ratelimited"
    assert second.headers["x-ratelimit-limit"] == "1"


def test_ac12_unknown_host_404_has_no_ingress_identity():
    plane = DataPlane(routing_config())
    result = asyncio.run(
        plane.handle(
            hostname="unknown.example.com",
            path="/",
            remote_addr="203.0.113.9",
        )
    )

    assert result.action == "notfound"
    assert result.status_code == 404
    headers = {key.lower(): value for key, value in result.headers.items()}
    assert "server" not in headers
    body = result.body.lower()
    for identity in ("nginx", "ingress", "uvicorn", "werkzeug", "python"):
        assert identity not in body
    assert "page not found" in body
