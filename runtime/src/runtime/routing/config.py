"""Pure whole-snapshot routing config rendering (SPEC-16 R-1/R-2).

The API never sends route-change events.  Each poll reads the complete rows,
reuses SPEC-15's servable-host filter, validates that every proxy/static route
has a backend, and builds one immutable config with a content fingerprint.  A
failure here raises before the edge is touched, which is what lets the
reconciler retain the last good config.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from runtime.ingress.models import IngressSnapshot, NotFoundResponse, RouteRule
from runtime.ingress.render import IngressConfig, servable_routes
from runtime.ingress.routing import (
    MAX_REWRITE_DEPTH,
    designed_not_found,
)
from runtime.routing.errors import RoutingRenderError
from runtime.routing.ratelimit import RateLimitPolicy
from runtime.routing.table import RouteTable

DEFAULT_RATE_LIMIT = RateLimitPolicy()


def _route_payload(route: RouteRule) -> dict[str, Any]:
    return {
        "id": route.id,
        "org_id": route.org_id,
        "project_id": route.project_id,
        "hostname": route.hostname,
        "path_prefix": route.path_prefix,
        "kind": route.kind,
        "target_path": route.target_path,
        "status_code": route.status_code,
        "priority": route.priority,
        "deployment_id": route.deployment_id,
        "revision": route.revision,
        "namespace": route.namespace,
        "service_name": route.service_name,
        "service_port": route.service_port,
    }


def routing_fingerprint(
    routes: Iterable[RouteRule],
    *,
    not_found: NotFoundResponse | None = None,
    rate_limit: RateLimitPolicy = DEFAULT_RATE_LIMIT,
    max_rewrites: int = MAX_REWRITE_DEPTH,
) -> str:
    """Stable content hash for an entire desired config.

    Route IDs are included so a changed row always produces a new revision;
    sorting makes the hash independent of database row order.
    """

    missing = designed_not_found() if not_found is None else not_found
    payload = {
        "format": 1,
        "max_rewrites": max_rewrites,
        "rate_limit": {
            "limit": rate_limit.limit,
            "window_seconds": rate_limit.window_seconds,
        },
        "not_found": {
            "status_code": missing.status_code,
            "body": missing.body,
            "headers": dict(sorted(missing.headers.items())),
        },
        "routes": [
            _route_payload(route)
            for route in sorted(
                routes,
                key=lambda route: (
                    route.hostname,
                    route.path_prefix,
                    -route.priority,
                    route.id,
                ),
            )
        ],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RoutingConfig:
    """The complete data-plane view for one revision."""

    table: RouteTable
    not_found: NotFoundResponse
    rate_limit: RateLimitPolicy = field(default_factory=RateLimitPolicy)
    max_rewrites: int = MAX_REWRITE_DEPTH
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_rewrites, bool)
            or not isinstance(self.max_rewrites, int)
            or self.max_rewrites < 1
        ):
            raise ValueError("max_rewrites must be a positive integer")
        if not self.fingerprint:
            object.__setattr__(
                self,
                "fingerprint",
                routing_fingerprint(
                    self.table.routes,
                    not_found=self.not_found,
                    rate_limit=self.rate_limit,
                    max_rewrites=self.max_rewrites,
                ),
            )

    @property
    def route_count(self) -> int:
        return len(self.table)


def render_routing_config(
    snapshot: IngressSnapshot,
    ingress_config: IngressConfig,
    *,
    rate_limit: RateLimitPolicy = DEFAULT_RATE_LIMIT,
    max_rewrites: int = MAX_REWRITE_DEPTH,
) -> RoutingConfig:
    """Render the full desired config from one complete snapshot.

    Unverified/suspended domains are filtered by ``servable_routes`` before they
    can reach the edge.
    """

    routes = servable_routes(snapshot, ingress_config)
    for route in routes:
        if route.kind in {"proxy", "static"} and not route.has_backend:
            raise RoutingRenderError(
                f"route {route.id!r} for {route.hostname!r} has no backend"
            )

    not_found = designed_not_found()
    table = RouteTable(routes)
    return RoutingConfig(
        table=table,
        not_found=not_found,
        rate_limit=rate_limit,
        max_rewrites=max_rewrites,
        fingerprint=routing_fingerprint(
            table.routes,
            not_found=not_found,
            rate_limit=rate_limit,
            max_rewrites=max_rewrites,
        ),
    )


__all__ = [
    "DEFAULT_RATE_LIMIT",
    "RoutingConfig",
    "render_routing_config",
    "routing_fingerprint",
]
