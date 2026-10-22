"""SPEC-16 AC-3..AC-6: indexed resolution semantics and the latency budget."""

from __future__ import annotations

import gc
import statistics
import time
from datetime import UTC, datetime

from runtime.ingress.models import DomainRecord, IngressSnapshot, RouteRule
from runtime.ingress.render import IngressConfig
from runtime.routing import RouteTable, render_routing_config


def route(
    route_id: str,
    host: str,
    *,
    path: str = "/",
    kind: str = "proxy",
    target: str | None = None,
    priority: int = 0,
    service: str | None = "web",
) -> RouteRule:
    return RouteRule(
        id=route_id,
        org_id="org-1",
        project_id="project-1",
        hostname=host,
        path_prefix=path,
        kind=kind,
        target_path=target,
        priority=priority,
        namespace="autopilot-org" if service else None,
        service_name=service,
        service_port=8080 if service else None,
    )


PREVIEW = "preview.example.com"


def preview_host(number: int) -> str:
    return f"pr-{number}-repo-deadbeef.{PREVIEW}"


def ingress_config() -> IngressConfig:
    return IngressConfig(preview_domain=PREVIEW)


def test_ac3_exact_health_path_wins_over_root_catch_all():
    host = preview_host(1)
    table = RouteTable(
        (
            route("root", host, path="/"),
            route("health", host, path="/health"),
        )
    )

    health = table.resolve(host, "/health")
    assert health.action == "proxy"
    assert health.route is not None
    assert health.route.id == "health"

    fallback = table.resolve(host, "/anything-else")
    assert fallback.action == "proxy"
    assert fallback.route is not None
    assert fallback.route.id == "root"


def test_ac4_wildcard_matches_below_but_never_the_apex():
    host = preview_host(1)
    table = RouteTable((route("wildcard", f"*.{PREVIEW}"),))

    below = table.resolve(host, "/")
    assert below.action == "proxy"
    assert below.route is not None
    assert below.route.id == "wildcard"

    apex = table.resolve(PREVIEW, "/")
    assert apex.action == "notfound"
    assert apex.status_code == 404


def test_ac5_rewrite_cycle_terminates_with_a_clear_error():
    host = preview_host(1)
    table = RouteTable(
        (
            route("a", host, path="/a", kind="rewrite", target="/b", service=None),
            route("b", host, path="/b", kind="rewrite", target="/a", service=None),
        )
    )

    result = table.resolve(host, "/a")
    assert result.action == "error"
    assert result.status_code == 500
    assert result.rewrite_depth <= 5
    assert result.message is not None
    assert "cycle" in result.message or "depth" in result.message


def test_ac5_a_long_rewrite_chain_stops_at_depth_five():
    host = preview_host(1)
    rules = [
        route(
            f"hop-{number}",
            host,
            path=f"/{number}",
            kind="rewrite",
            target=f"/{number + 1}",
            service=None,
        )
        for number in range(6)
    ]
    rules.append(route("terminal", host, path="/6"))
    table = RouteTable(tuple(rules))

    result = table.resolve(host, "/0")
    assert result.action == "error"
    assert result.status_code == 500
    assert result.rewrite_depth == 5
    assert result.message is not None
    assert "depth limit 5" in result.message


def test_exact_host_wins_over_a_wildcard_row():
    host = preview_host(1)
    table = RouteTable(
        (
            route("wildcard", f"*.{PREVIEW}"),
            route("exact", host),
        )
    )

    result = table.resolve(host, "/")
    assert result.route is not None
    assert result.route.id == "exact"


def test_indexed_candidates_are_scoped_to_the_hostname():
    host = preview_host(1)
    other = preview_host(2)
    table = RouteTable(
        (
            route("exact", host),
            route("wildcard", f"*.{PREVIEW}"),
            route("other", other),
        )
    )

    candidate_ids = {item.id for item in table.candidates(host)}
    assert candidate_ids == {"exact", "wildcard"}
    assert table.candidates("unknown.example.com") == ()
    assert table.host_count == 3  # two exact hosts plus one wildcard suffix


def test_unservable_custom_domain_never_reaches_the_config():
    suspended = DomainRecord(
        id="domain-1",
        org_id="org-1",
        project_id="project-1",
        hostname="suspended.example.com",
        status="suspended",
        cert_issued_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    snapshot = IngressSnapshot(
        routes=(route("suspended-route", "suspended.example.com"),),
        domains=(suspended,),
    )

    rendered = render_routing_config(snapshot, ingress_config())
    assert rendered.route_count == 0
    result = rendered.table.resolve("suspended.example.com", "/")
    assert result.action == "notfound"


def test_ac6_p99_resolution_is_below_100_microseconds():
    hosts = [preview_host(number) for number in range(2000)]
    routes: list[RouteRule] = []
    for number, host in enumerate(hosts):
        routes.append(route(f"root-{number}", host))
        routes.append(route(f"health-{number}", host, path="/health"))

    table = RouteTable(tuple(routes))
    assert len(table) == 4000

    samples: list[tuple[str, str]] = []
    for number, host in enumerate(hosts):
        if number % 10 == 0:
            samples.append((host, "/health"))
        else:
            samples.append((host, "/some/deep/path"))
    samples.extend(("unknown.example.com", "/") for _ in range(200))

    for host, path in samples[:500]:
        table.resolve(host, path)

    # Take the median of five runs per request.  A single scheduler stall on a
    # shared CI host can otherwise dominate an individual sample; the median
    # still measures the routing decision that produced it.
    medians: list[float] = []
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        for host, path in samples:
            timings = []
            for _ in range(5):
                started = time.perf_counter_ns()
                table.resolve(host, path)
                timings.append(time.perf_counter_ns() - started)
            medians.append(statistics.median(timings))
    finally:
        if gc_was_enabled:
            gc.enable()

    ordered = sorted(medians)
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    assert p99 < 100_000, f"p99 was {p99:.0f} ns across {len(medians)} requests"
