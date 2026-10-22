"""Hostname-indexed, immutable route table (SPEC-16 R-14/R-15).

``runtime.ingress.routing.resolve_prefiltered`` remains the one implementation
of the evaluation order.  This table avoids scanning every route for every
request: it canonicalises route patterns once at build time and keeps a sorted
slice per exact hostname, plus wildcard slices keyed by parent suffix.  A
request only sees candidates for its hostname; rewrite re-resolution still
happens inside the shared resolver with the same host.

The table is immutable after construction (mappings are read-only proxies).
Reconcilers replace a whole table/config rather than mutating it in place.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from runtime.ingress.models import Resolution, RouteRule
from runtime.ingress.routing import MAX_REWRITE_DEPTH, resolve_prefiltered
from runtime.ingress.validation import canonical_hostname, is_below_suffix


def _route_sort_key(route: RouteRule) -> tuple[str, int, str, int, str]:
    return (
        route.hostname,
        -len(route.path_prefix),
        route.path_prefix,
        -route.priority,
        route.id,
    )


def _host_sort_key(route: RouteRule) -> tuple[int, int, str]:
    return (-len(route.path_prefix), -route.priority, route.id)


class RouteTable:
    """A pre-indexed view over the route rows for one desired-state snapshot."""

    __slots__ = ("_exact", "_wildcards", "routes")

    def __init__(self, routes: Iterable[RouteRule]) -> None:
        ordered = tuple(sorted(routes, key=_route_sort_key))
        exact: dict[str, list[RouteRule]] = {}
        wildcard: dict[str, list[RouteRule]] = {}

        for route in ordered:
            if route.hostname.startswith("*."):
                suffix = canonical_hostname(route.hostname[2:])
                wildcard.setdefault(suffix, []).append(route)
            else:
                host = canonical_hostname(route.hostname)
                exact.setdefault(host, []).append(route)

        self.routes = ordered
        self._exact: Mapping[str, tuple[RouteRule, ...]] = MappingProxyType(
            {
                host: tuple(sorted(host_routes, key=_host_sort_key))
                for host, host_routes in exact.items()
            }
        )
        self._wildcards: tuple[tuple[str, tuple[RouteRule, ...]], ...] = tuple(
            (
                suffix,
                tuple(sorted(wildcard[suffix], key=_host_sort_key)),
            )
            for suffix in sorted(wildcard, key=lambda value: (-len(value), value))
        )

    def __len__(self) -> int:
        return len(self.routes)

    @property
    def host_count(self) -> int:
        return len(self._exact) + len(self._wildcards)

    def _candidates_for_canonical(self, host: str) -> tuple[RouteRule, ...]:
        exact = self._exact.get(host, ())
        if not self._wildcards:
            return exact

        matched: list[RouteRule] = []
        for suffix, host_routes in self._wildcards:
            if is_below_suffix(host, suffix):
                matched.extend(host_routes)
        if not matched:
            return exact
        if not exact:
            return tuple(matched)
        return exact + tuple(matched)

    def candidates(self, hostname: str) -> tuple[RouteRule, ...]:
        """Return the exact-host and matching wildcard rows for ``hostname``."""

        return self._candidates_for_canonical(canonical_hostname(hostname))

    def resolve(
        self,
        hostname: str,
        path: str,
        *,
        max_rewrites: int = MAX_REWRITE_DEPTH,
    ) -> Resolution:
        """Resolve one request using only this hostname's rows."""

        host = canonical_hostname(hostname)
        return resolve_prefiltered(
            self._candidates_for_canonical(host),
            hostname=host,
            path=path,
            max_rewrites=max_rewrites,
        )


__all__ = ["RouteTable"]
