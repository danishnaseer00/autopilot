"""SPEC-16 AC-1, AC-2, AC-10 and AC-11: pure polling convergence."""

from __future__ import annotations

import asyncio

from runtime.ingress import (
    FakeIngressApi,
    IngressReconciler,
    object_key,
    render_ingress,
)
from runtime.ingress.models import IngressSnapshot, RouteRule
from runtime.ingress.render import IngressConfig
from runtime.routing import (
    InMemoryEdgeConfigApi,
    RoutingConfig,
    RoutingReconciler,
)

PREVIEW = "preview.example.com"


def route(route_id: str, host: str, *, service: str | None = "web") -> RouteRule:
    return RouteRule(
        id=route_id,
        org_id="org-1",
        project_id="project-1",
        hostname=host,
        path_prefix="/",
        kind="proxy",
        namespace="autopilot-org" if service else None,
        service_name=service,
        service_port=8080 if service else None,
    )


def preview_host(number: int) -> str:
    return f"pr-{number}-repo-deadbeef.{PREVIEW}"


def snapshot(*hosts: str) -> IngressSnapshot:
    return IngressSnapshot(routes=tuple(route(f"route-{host}", host) for host in hosts))


class StaticSource:
    def __init__(self, value: IngressSnapshot) -> None:
        self.value = value
        self.error: Exception | None = None
        self.calls = 0

    async def snapshot(self) -> IngressSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class SequenceSource:
    def __init__(self, values: list[IngressSnapshot]) -> None:
        self.values = values
        self.calls = 0

    async def snapshot(self) -> IngressSnapshot:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return self.values[index]


def build(source, edge=None):
    ingress = IngressConfig(preview_domain=PREVIEW)
    edge = edge or InMemoryEdgeConfigApi()
    return RoutingReconciler(source, edge, ingress, poll_interval=0), edge


def test_ac1_deleted_ingress_objects_are_rebuilt_in_one_ingress_poll():
    # The object-rendering reconciler is shared with SPEC-15.  SPEC-16's
    # routing config is layered on the same full desired-state snapshot; this
    # keeps AC-1's literal Ingress-object assertion in the routing suite too.
    source = StaticSource(snapshot(preview_host(1), preview_host(2)))
    ingress = IngressConfig(preview_domain=PREVIEW)
    api = FakeIngressApi()
    reconciler = IngressReconciler(source, api, ingress, poll_interval_seconds=0.01)

    asyncio.run(reconciler.run_once())
    expected = {object_key(obj) for obj in render_ingress(source.value, ingress)}
    assert set(api.keys()) == expected

    assert api.delete_all_managed() > 0
    assert api.keys() == ()

    asyncio.run(reconciler.run_once())
    assert set(api.keys()) == expected


def test_ac1_a_deleted_edge_config_is_rebuilt_in_one_poll():
    source = StaticSource(snapshot(preview_host(1), preview_host(2)))
    reconciler, edge = build(source)

    first = asyncio.run(reconciler.run_once())
    assert first.changed is True
    assert edge.current is not None
    first_fingerprint = edge.current.fingerprint

    edge.clear()
    assert edge.current is None

    second = asyncio.run(reconciler.run_once())
    assert second.changed is True
    assert second.failed is False
    assert edge.current is not None
    assert edge.current.fingerprint == first_fingerprint
    assert edge.changes == 2


def test_ac2_a_second_poll_with_the_same_snapshot_changes_nothing():
    source = StaticSource(snapshot(preview_host(1)))
    reconciler, edge = build(source)

    asyncio.run(reconciler.run_once())
    assert edge.apply_calls == 1

    second = asyncio.run(reconciler.run_once())
    assert second.failed is False
    assert second.changed is False
    assert edge.apply_calls == 1
    assert edge.changes == 1


def test_ac10_render_failure_retains_the_last_good_config_without_applying():
    source = StaticSource(snapshot(preview_host(1)))
    reconciler, edge = build(source)
    asyncio.run(reconciler.run_once())
    assert edge.current is not None
    before = edge.current
    apply_calls = edge.apply_calls

    source.error = RuntimeError("postgres snapshot unavailable")
    failed = asyncio.run(reconciler.run_once())
    assert failed.failed is True
    assert failed.retained is True
    assert failed.error is not None and "postgres snapshot" in failed.error
    assert edge.current is before
    assert edge.apply_calls == apply_calls

    source.error = None
    source.value = IngressSnapshot(
        routes=(route("missing-backend", preview_host(2), service=None),)
    )
    render_failed = asyncio.run(reconciler.run_once())
    assert render_failed.failed is True
    assert render_failed.retained is True
    assert render_failed.error is not None
    assert "has no backend" in render_failed.error
    assert edge.current is before
    assert edge.apply_calls == apply_calls
    assert reconciler.last_good is before


def test_ac11_two_polls_converge_after_a_route_change_without_events():
    first_snapshot = snapshot(preview_host(1))
    second_snapshot = snapshot(preview_host(2))
    source = SequenceSource([first_snapshot, second_snapshot])
    reconciler, edge = build(source)

    results = asyncio.run(reconciler.run_forever(max_cycles=2, interval_seconds=0))

    assert len(results) == 2
    assert source.calls == 2
    assert results[0].changed is True
    assert results[1].changed is True
    assert edge.current is not None
    assert edge.current.table.resolve(preview_host(2), "/").action == "proxy"
    assert edge.current.table.resolve(preview_host(1), "/").action == "notfound"


def test_apply_failure_is_retried_with_the_whole_config():
    class FlakyEdge(InMemoryEdgeConfigApi):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        async def apply(self, config: RoutingConfig) -> None:
            if self.failures:
                self.failures -= 1
                raise RuntimeError("edge apply interrupted")
            await super().apply(config)

    source = StaticSource(snapshot(preview_host(1)))
    edge = FlakyEdge()
    reconciler, edge = build(source, edge)

    failed = asyncio.run(reconciler.run_once())
    assert failed.failed is True
    assert edge.current is None

    repaired = asyncio.run(reconciler.run_once())
    assert repaired.failed is False
    assert repaired.changed is True
    assert edge.current is not None
    assert edge.current.table.resolve(preview_host(1), "/").action == "proxy"
