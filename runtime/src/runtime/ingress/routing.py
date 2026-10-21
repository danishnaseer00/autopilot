"""SPEC-15 R-12 / SPEC-16 R-6 route resolution.

This is a pure in-memory function, not an HTTP server.  The data plane can call
it for every request without a database round trip (SPEC-16 R-13).  Routes are
loaded from the ``routes`` table and hostnames are matched by table lookup; no
hostname is ever parsed into a deployment id (SPEC-15 R-14).

Evaluation order is load-bearing:

1. redirects (never shadowed by a proxy rule)
2. rewrites (then re-resolve, bounded depth 5 with a visited set)
3. exact path before prefix
4. longest prefix
5. priority at equal path length
6. designed 404
"""

from __future__ import annotations

from typing import Iterable, Sequence

from runtime.ingress.errors import RewriteDepthExceeded
from runtime.ingress.models import NotFoundResponse, Resolution, RouteRule
from runtime.ingress.validation import (
    canonical_hostname,
    is_below_suffix,
    validate_path,
)

MAX_REWRITE_DEPTH = 5

_NOT_FOUND_BODY = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    "<title>Page not found</title></head><body><main>"
    "<h1>Page not found</h1>"
    "<p>This address is not configured for a site.</p>"
    "</main></body></html>"
)
_NOT_FOUND_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
}


def designed_not_found() -> NotFoundResponse:
    """R-11: a deliberately boring 404 with no controller identity or version."""

    return NotFoundResponse(
        status_code=404,
        body=_NOT_FOUND_BODY,
        headers=dict(_NOT_FOUND_HEADERS),
    )


def match_host(pattern: str, hostname: str) -> bool:
    """R-13: a wildcard matches names *below* the suffix, never the suffix."""

    try:
        host = canonical_hostname(hostname)
    except ValueError:
        return False
    try:
        pattern = pattern.lower().rstrip(".")
        if pattern.startswith("*."):
            return is_below_suffix(host, pattern[2:])
        return canonical_hostname(pattern) == host
    except ValueError:
        return False


def match_path(prefix: str, path: str) -> bool:
    return path.startswith(prefix)


def _host_specificity(route: RouteRule, hostname: str) -> int:
    """Exact hostnames beat wildcard patterns; then priority and id decide.

    The database's unique key is ``(hostname, path_prefix)``, so wildcard and
    exact-host routes can coexist.  Choosing the exact host here prevents a
    wildcard rule from shadowing an explicitly configured tenant hostname.
    """

    return 0 if route.hostname == hostname else 1


def _select(
    candidates: Sequence[RouteRule],
    *,
    hostname: str,
    path: str,
) -> RouteRule:
    exact = [route for route in candidates if route.path_prefix == path]
    if exact:
        pool = exact
    else:
        longest = max(len(route.path_prefix) for route in candidates)
        pool = [route for route in candidates if len(route.path_prefix) == longest]

    return min(
        pool,
        key=lambda route: (
            _host_specificity(route, hostname),
            -route.priority,
            -len(route.path_prefix),
            route.id,
        ),
    )


def _redirect_location(route: RouteRule) -> str:
    target = route.target_path or "/"
    # Absolute targets keep their own host; relative targets replace the path
    # on the current host.  Both are declared in the routes table.
    return target


def _apply_rewrite(path: str, route: RouteRule) -> str:
    target = route.target_path or "/"
    if target.startswith(("http://", "https://")):
        # A rewrite is internal; an absolute target would be an open redirect
        # disguised as routing.  Model validation already rejects this.
        raise ValueError("rewrite target must be a path")
    if target.endswith("/") and path != route.path_prefix:
        suffix = path[len(route.path_prefix) :]
        return target.rstrip("/") + "/" + suffix.lstrip("/")
    return target


def resolve_request(
    routes: Iterable[RouteRule],
    hostname: str,
    path: str,
    *,
    max_rewrites: int = MAX_REWRITE_DEPTH,
) -> Resolution:
    """Resolve one request to exactly one route, redirect or 404.

    Raises :class:`RewriteDepthExceeded` only if the caller wants the bounded
    error as an exception; the returned response for a cycle is a 500 with a
    clear message, matching SPEC-15 §5 and AC-7.
    """

    host = canonical_hostname(hostname)
    current = validate_path(path)
    table = tuple(routes)
    visited: set[tuple[str, str]] = set()
    depth = 0

    while True:
        key = (host, current)
        if key in visited:
            message = (
                f"rewrite cycle detected for {host!r} at depth {depth}; "
                f"depth limit {max_rewrites}"
            )
            return Resolution(
                action="error",
                status_code=500,
                hostname=host,
                path=current,
                message=message,
                rewrite_depth=depth,
                headers={"content-type": "text/plain; charset=utf-8"},
                body=message + "\n",
            )
        visited.add(key)

        candidates = [
            route
            for route in table
            if match_host(route.hostname, host) and match_path(route.path_prefix, current)
        ]

        redirects = [route for route in candidates if route.kind == "redirect"]
        if redirects:
            chosen = _select(redirects, hostname=host, path=current)
            return Resolution(
                action="redirect",
                status_code=chosen.status_code,
                hostname=host,
                path=current,
                route=chosen,
                location=_redirect_location(chosen),
                rewrite_depth=depth,
            )

        rewrites = [route for route in candidates if route.kind == "rewrite"]
        if rewrites:
            chosen = _select(rewrites, hostname=host, path=current)
            if depth >= max_rewrites:
                message = (
                    f"rewrite depth limit {max_rewrites} exceeded for {host!r}; "
                    "the route set contains a cycle"
                )
                return Resolution(
                    action="error",
                    status_code=500,
                    hostname=host,
                    path=current,
                    route=chosen,
                    message=message,
                    rewrite_depth=depth,
                    headers={"content-type": "text/plain; charset=utf-8"},
                    body=message + "\n",
                )
            next_path = _apply_rewrite(current, chosen)
            if (host, next_path) in visited:
                message = (
                    f"rewrite cycle detected for {host!r} at depth {depth + 1}; "
                    f"depth limit {max_rewrites}"
                )
                return Resolution(
                    action="error",
                    status_code=500,
                    hostname=host,
                    path=next_path,
                    route=chosen,
                    message=message,
                    rewrite_depth=depth + 1,
                    headers={"content-type": "text/plain; charset=utf-8"},
                    body=message + "\n",
                )
            current = next_path
            depth += 1
            continue

        terminals = [
            route
            for route in candidates
            if route.kind in {"proxy", "static", "notfound"}
        ]
        if not terminals:
            not_found = designed_not_found()
            return Resolution(
                action="notfound",
                status_code=not_found.status_code,
                hostname=host,
                path=current,
                rewrite_depth=depth,
                headers=not_found.headers,
                body=not_found.body,
            )

        chosen = _select(terminals, hostname=host, path=current)
        if chosen.kind == "notfound":
            not_found = designed_not_found()
            return Resolution(
                action="notfound",
                status_code=not_found.status_code,
                hostname=host,
                path=current,
                route=chosen,
                rewrite_depth=depth,
                headers=not_found.headers,
                body=not_found.body,
            )
        return Resolution(
            action=chosen.kind,
            status_code=200,
            hostname=host,
            path=current,
            route=chosen,
            rewrite_depth=depth,
        )


def resolve_or_error(
    routes: Iterable[RouteRule], hostname: str, path: str
) -> Resolution:
    """Like :func:`resolve_request` but a bounded rewrite cycle raises."""

    result = resolve_request(routes, hostname, path)
    if result.action == "error":
        raise RewriteDepthExceeded(
            depth=result.rewrite_depth,
            limit=MAX_REWRITE_DEPTH,
            hostname=result.hostname,
        )
    return result


__all__ = [
    "MAX_REWRITE_DEPTH",
    "designed_not_found",
    "match_host",
    "match_path",
    "resolve_or_error",
    "resolve_request",
]
