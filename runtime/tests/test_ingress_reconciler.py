"""SPEC-15 AC-9 convergence, idempotence and last-good retention."""

from __future__ import annotations

import asyncio

from runtime.ingress import (
    FakeIngressApi,
    IngressConfig,
    IngressReconciler,
    IngressSnapshot,
    RouteRule,
    ingress_name,
    object_key,
    render_ingress,
)


def route(route_id: str, host: str) -> RouteRule:
    return RouteRule(
        id=route_id,
        org_id="org-1",
        project_id="project-1",
        hostname=host,
        path_prefix="/",
        kind="proxy",
        namespace="autopilot-org",
        service_name="web",
        service_port=8080,
    )


def snapshot(*hosts: str) -> IngressSnapshot:
    return IngressSnapshot(
        routes=tuple(
            route(f"route-{host}", host) for host in hosts
        )
    )


class StaticSource:
    def __init__(self, value: IngressSnapshot) -> None:
        self.value = value
        self.error: Exception | None = None

    async def snapshot(self) -> IngressSnapshot:
        if self.error is not None:
            raise self.error
        return self.value


def build(source: StaticSource):
    config = IngressConfig(
        preview_domain="preview.example.com",
        include_not_found_backend=True,
    )
    api = FakeIngressApi()
    return IngressReconciler(source, api, config), api, config


def expected_keys(config: IngressConfig, value: IngressSnapshot):
    return {object_key(obj) for obj in render_ingress(value, config)}


def test_ac9_deleted_ingresses_are_recreated_in_one_reconciliation_pass():
    hosts = (
        "pr-1-repo-deadbeef.preview.example.com",
        "pr-2-repo-deadbeef.preview.example.com",
        "pr-3-repo-deadbeef.preview.example.com",
    )
    source = StaticSource(snapshot(*hosts))
    reconciler, api, config = build(source)

    first = asyncio.run(reconciler.run_once())
    assert first.failed is False
    assert first.applied == len(expected_keys(config, source.value))
    assert set(api.keys()) == expected_keys(config, source.value)

    deleted = api.delete_all_managed()
    assert deleted > 0
    assert api.keys() == ()

    # One poll interval later, with no event and no database change, the full
    # desired state is applied again.
    second = asyncio.run(reconciler.run_once())
    assert second.failed is False
    assert set(api.keys()) == expected_keys(config, source.value)
    assert second.changed > 0


def test_second_pass_is_idempotent_and_does_not_rewrite_objects():
    source = StaticSource(snapshot("pr-1-repo-deadbeef.preview.example.com"))
    reconciler, api, _ = build(source)
    asyncio.run(reconciler.run_once())
    changes_after_first = api.changes
    second = asyncio.run(reconciler.run_once())
    assert second.failed is False
    assert second.changed == 0
    assert api.changes == changes_after_first


def test_render_failure_retains_the_last_good_state():
    source = StaticSource(snapshot("pr-1-repo-deadbeef.preview.example.com"))
    reconciler, api, _ = build(source)
    asyncio.run(reconciler.run_once())
    before = api.keys()

    source.error = RuntimeError("database snapshot unavailable")
    result = asyncio.run(reconciler.run_once())
    assert result.failed is True
    assert "database snapshot unavailable" in (result.error or "")
    assert api.keys() == before


def test_partial_apply_is_repaired_without_deleting_previous_objects():
    hosts = (
        "pr-1-repo-deadbeef.preview.example.com",
        "pr-2-repo-deadbeef.preview.example.com",
    )
    source = StaticSource(snapshot(*hosts))
    reconciler, api, config = build(source)

    api.fail_apply_after = 1
    first = asyncio.run(reconciler.run_once())
    assert first.failed is True
    partial_keys = set(api.keys())
    assert partial_keys

    api.fail_apply_after = None
    second = asyncio.run(reconciler.run_once())
    assert second.failed is False
    assert set(api.keys()) >= partial_keys
    assert set(api.keys()) == expected_keys(config, source.value)


def test_reconciler_does_not_delete_other_managed_plane_objects():
    source = StaticSource(snapshot("pr-1-repo-deadbeef.preview.example.com"))
    reconciler, api, _ = build(source)
    foreign_key = api.seed(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": "web",
                "namespace": "autopilot-org",
                "labels": {"autopilot/managed": "true"},
            },
            "spec": {},
        }
    )
    result = asyncio.run(reconciler.run_once())
    assert result.failed is False
    assert foreign_key in api.keys()


def test_a_route_removed_from_the_table_deletes_its_managed_ingress():
    source = StaticSource(
        snapshot(
            "pr-1-repo-deadbeef.preview.example.com",
            "pr-2-repo-deadbeef.preview.example.com",
        )
    )
    reconciler, api, config = build(source)
    asyncio.run(reconciler.run_once())
    removed_key = (
        "Ingress",
        "autopilot-org",
        ingress_name("pr-2-repo-deadbeef.preview.example.com"),
    )
    assert removed_key in api.keys()

    source.value = snapshot("pr-1-repo-deadbeef.preview.example.com")
    result = asyncio.run(reconciler.run_once())
    assert result.failed is False
    assert result.deleted == 1
    assert removed_key not in api.keys()
    assert set(api.keys()) == expected_keys(config, source.value)
