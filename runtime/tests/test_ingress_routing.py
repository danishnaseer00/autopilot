"""SPEC-15 AC-6/7/8 and the shared route-resolution semantics."""

from __future__ import annotations

from runtime.ingress import (
    RouteRule,
    designed_not_found,
    resolve_or_error,
    resolve_request,
)
from runtime.ingress.domains import plan_onboarding
from runtime.ingress.errors import RewriteDepthExceeded


def route(
    route_id: str,
    *,
    host: str = "app.example.com",
    path: str = "/",
    kind: str = "proxy",
    target: str | None = None,
    status: int = 200,
    priority: int = 0,
    service: str = "web",
) -> RouteRule:
    return RouteRule(
        id=route_id,
        org_id="org-1",
        project_id="project-1",
        hostname=host,
        path_prefix=path,
        kind=kind,
        target_path=target,
        status_code=status,
        priority=priority,
        namespace="autopilot-org",
        service_name=service if kind in {"proxy", "static"} else None,
        service_port=8080 if kind in {"proxy", "static"} else None,
    )


def test_ac6_exact_path_beats_prefix_and_health_route_wins():
    routes = [
        route("root", path="/"),
        route("health", path="/health", service="health"),
    ]
    result = resolve_request(routes, "app.example.com", "/health")
    assert result.action == "proxy"
    assert result.route is not None and result.route.id == "health"
    assert result.status_code == 200

    fallback = resolve_request(routes, "app.example.com", "/health/extra")
    assert fallback.route is not None and fallback.route.id == "health"

    other = resolve_request(routes, "app.example.com", "/another")
    assert other.route is not None and other.route.id == "root"


def test_redirects_are_evaluated_before_proxy_and_before_rewrites():
    routes = [
        route("root", path="/", kind="proxy"),
        route("old", path="/old", kind="redirect", target="/new", status=301),
        route("rewrite", path="/old", kind="rewrite", target="/elsewhere"),
    ]
    result = resolve_request(routes, "app.example.com", "/old/deep")
    assert result.action == "redirect"
    assert result.status_code == 301
    assert result.location == "/new"
    assert result.route is not None and result.route.id == "old"


def test_rewrite_re_resolves_and_preserves_the_suffix():
    routes = [
        route("rewrite", path="/old", kind="rewrite", target="/new/"),
        route("new", path="/new/", kind="proxy", service="new"),
    ]
    result = resolve_request(routes, "app.example.com", "/old/deep")
    assert result.action == "proxy"
    assert result.path == "/new/deep"
    assert result.route is not None and result.route.id == "new"
    assert result.rewrite_depth == 1


def test_ac7_rewrite_cycle_is_bounded_at_five_with_a_clear_error():
    routes = [
        route("a", path="/a", kind="rewrite", target="/b"),
        route("b", path="/b", kind="rewrite", target="/a"),
        route("root", path="/", kind="proxy"),
    ]
    result = resolve_request(routes, "app.example.com", "/a")
    assert result.action == "error"
    assert result.status_code == 500
    assert result.rewrite_depth <= 5
    assert "rewrite" in (result.message or "")
    assert "5" in (result.message or "")

    try:
        resolve_or_error(routes, "app.example.com", "/a")
    except RewriteDepthExceeded as exc:
        assert exc.limit == 5
        assert exc.depth <= 5
    else:  # pragma: no cover
        raise AssertionError("cycle did not raise")


def test_rewrite_chain_of_exactly_five_is_allowed():
    routes = [
        route("r1", path="/1", kind="rewrite", target="/2"),
        route("r2", path="/2", kind="rewrite", target="/3"),
        route("r3", path="/3", kind="rewrite", target="/4"),
        route("r4", path="/4", kind="rewrite", target="/5"),
        route("r5", path="/5", kind="rewrite", target="/done"),
        route("done", path="/done", kind="proxy"),
    ]
    result = resolve_request(routes, "app.example.com", "/1")
    assert result.action == "proxy"
    assert result.path == "/done"
    assert result.rewrite_depth == 5


def test_priority_breaks_equal_length_prefix_ties_deterministically():
    routes = [
        route("low", path="/api", priority=1, service="low"),
        route("high", path="/api", priority=9, service="high"),
    ]
    result = resolve_request(routes, "app.example.com", "/api/users")
    assert result.route is not None and result.route.id == "high"


def test_exact_hostname_beats_a_wildcard_at_the_same_path():
    routes = [
        route("wild", host="*.example.com", path="/", service="wild"),
        route("exact", host="app.example.com", path="/", service="exact"),
    ]
    result = resolve_request(routes, "app.example.com", "/")
    assert result.route is not None and result.route.id == "exact"


def test_ac8_wildcard_pattern_does_not_match_the_apex():
    routes = [route("wild", host="*.preview.example.com", path="/")]
    apex = resolve_request(routes, "preview.example.com", "/")
    child = resolve_request(routes, "pr-1-repo-deadbeef.preview.example.com", "/")

    assert apex.action == "notfound"
    assert apex.status_code == 404
    assert child.action == "proxy"


def test_unmatched_host_returns_the_designed_404():
    result = resolve_request([], "unknown.example.net", "/anything")
    assert result.action == "notfound"
    assert result.status_code == 404
    assert "Page not found" in result.body
    response = designed_not_found()
    blob = response.body + "".join(
        f"{key}:{value}" for key, value in response.headers.items()
    )
    lowered = blob.lower()
    for forbidden in ("nginx", "ingress", "kubernetes", "server: "):
        assert forbidden not in lowered


def test_ac5_onboarding_plan_resolves_apex_and_www():
    plan = plan_onboarding(
        hostname="example.com",
        org_id="org-1",
        project_id="project-1",
        namespace="autopilot-org",
        tokens={"example.com": "tok-apex", "www.example.com": "tok-www"},
        id_factory=iter(["apex-route", "www-route"]).__next__,
    )
    routes = [plan.apex_route, plan.www_route]
    apex = resolve_request(routes, "example.com", "/")
    www = resolve_request(routes, "www.example.com", "/pricing")

    assert apex.action == "proxy"
    assert www.action == "redirect"
    assert www.status_code == 301
    assert www.location == "https://example.com"
