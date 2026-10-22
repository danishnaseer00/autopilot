"""SPEC-16 routing: indexed resolution, edge config convergence and limits.

The resolver in :mod:`runtime.ingress.routing` is the single implementation of
the redirect/rewrite/path/404 semantics.  This package adds the pieces the data
plane needs around it: a hostname-indexed table, a pure whole-snapshot config
renderer, a polling reconciler that retains last-good config, trusted-proxy
attribution, a fail-open fixed-window limiter and the serving facade.
"""

from runtime.routing.config import (
    DEFAULT_RATE_LIMIT,
    RoutingConfig,
    render_routing_config,
    routing_fingerprint,
)
from runtime.routing.errors import RoutingError, RoutingRenderError
from runtime.routing.models import RoutingReconcileResult
from runtime.routing.ratelimit import (
    InMemoryRateLimitStore,
    ProxyTrust,
    RateLimitDecision,
    RateLimiter,
    RateLimitPolicy,
    RateLimitStore,
)
from runtime.routing.reconcile import (
    EdgeConfigApi,
    InMemoryEdgeConfigApi,
    RoutingReconciler,
    RoutingSource,
)
from runtime.routing.serve import DataPlane
from runtime.routing.table import RouteTable

__all__ = [
    "DEFAULT_RATE_LIMIT",
    "DataPlane",
    "EdgeConfigApi",
    "InMemoryEdgeConfigApi",
    "InMemoryRateLimitStore",
    "ProxyTrust",
    "RateLimitDecision",
    "RateLimitPolicy",
    "RateLimitStore",
    "RateLimiter",
    "RouteTable",
    "RoutingConfig",
    "RoutingError",
    "RoutingReconcileResult",
    "RoutingReconciler",
    "RoutingRenderError",
    "RoutingSource",
    "render_routing_config",
    "routing_fingerprint",
]
