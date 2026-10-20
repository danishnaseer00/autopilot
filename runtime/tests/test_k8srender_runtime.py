"""SPEC-14 runtime orchestration against the fake cluster boundary.

AC-8 (safe teardown) and AC-9 (failed rollout does not promote) are fully
testable here. AC-9's cluster half - the old ReplicaSet actually keeps serving -
is for the coordinator; this test proves the runtime never calls promote.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from runtime.k8srender import RenderContext, RolloutFailed, render_manifest
from runtime.k8srender.cluster import FakeCluster
from runtime.k8srender.context import RenderContext as Context
from runtime.k8srender.deploy import DeploymentRuntime, delete_project
from runtime.k8srender.names import (
    MANAGED_LABEL,
    ORG_LABEL,
    PROJECT_LABEL,
    REVISION_LABEL,
    SERVICE_LABEL,
    label_value,
    namespace_for_org,
)
from runtime.lifecycle.models import Deployment, PromotionResult
from runtime.lifecycle.state import DrainPolicy

ORG_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"
DEPLOYMENT_ID = "33333333-3333-3333-3333-333333333333"
REVISION = "sha256:" + "a" * 64
IMAGE = "registry.platform.internal/autopilot/acme/shop@sha256:" + "b" * 64


def context() -> RenderContext:
    return Context(
        org_id=ORG_ID,
        project_id=PROJECT_ID,
        deployment_id=DEPLOYMENT_ID,
        revision=REVISION,
        namespace=namespace_for_org("acme"),
        project_slug="shop",
        runtime_image=IMAGE,
        plan_limits={"maxCpu": "2", "maxMemory": "4Gi"},
    )


def manifest() -> dict:
    return {
        "version": 1,
        "name": "acme-shop",
        "services": [
            {
                "name": "web",
                "source": ".",
                "type": "node",
                "build": {"strategy": "auto"},
                "runtime": {"port": 3000},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"}
                },
                "env": {"API_KEY": "${API_KEY}"},
            }
        ],
    }


def deployment() -> Deployment:
    now = datetime.now(timezone.utc)
    return Deployment(
        id=uuid.UUID(DEPLOYMENT_ID),
        org_id=uuid.UUID(ORG_ID),
        project_id=uuid.UUID(PROJECT_ID),
        git_sha="a" * 40,
        git_ref="refs/heads/main",
        environment="production",
        status="ready",
        stage="deploy",
        manifest=manifest(),
        manifest_hash="sha256:" + "c" * 64,
        build_image="ghcr.io/autopilot/build-node@" + "sha256:" + "d" * 64,
        build_env_keys=(),
        revision=REVISION,
        image_digest="sha256:" + "b" * 64,
        exit_code=0,
        error_message=None,
        created_at=now,
        started_at=now,
        finished_at=now,
        superseded_by=None,
    )


class RecordingLifecycle:
    def __init__(self, cluster: FakeCluster) -> None:
        self.cluster = cluster
        self.calls: list[tuple[str, dict]] = []

    async def promote(self, conn, **kwargs) -> PromotionResult:
        self.calls.append(
            ("promote", {"kwargs": kwargs, "cluster_calls": self.cluster.call_names()})
        )
        return PromotionResult(
            deployment=deployment(),
            routes_updated=1,
            superseded=(),
            changed=True,
        )

    async def mark_failed(self, conn, **kwargs) -> Deployment:
        self.calls.append(("mark_failed", kwargs))
        return deployment()

    async def rollback(self, conn, **kwargs) -> PromotionResult:
        self.calls.append(("rollback", kwargs))
        return PromotionResult(
            deployment=deployment(),
            routes_updated=1,
            superseded=(),
            changed=True,
        )


def test_apply_wait_then_promote_in_that_order() -> None:
    cluster = FakeCluster()
    lifecycle = RecordingLifecycle(cluster)
    runtime = DeploymentRuntime(cluster, lifecycle=lifecycle)  # type: ignore[arg-type]

    import asyncio

    async def scenario():
        return await runtime.deploy_and_promote(
            object(),  # type: ignore[arg-type]
            deployment=deployment(),
            ctx=context(),
            actor_id="worker-b",
        )

    result = asyncio.run(scenario())
    assert result.routes_updated == 1
    assert cluster.call_names() == ("apply", "wait_rollout")
    assert lifecycle.calls[0][0] == "promote"
    # At the moment promote ran, apply and wait had already completed.
    assert lifecycle.calls[0][1]["cluster_calls"] == ("apply", "wait_rollout")


def test_failed_rollout_marks_failed_and_never_promotes() -> None:
    cluster = FakeCluster()
    lifecycle = RecordingLifecycle(cluster)
    runtime = DeploymentRuntime(cluster, lifecycle=lifecycle)  # type: ignore[arg-type]
    workload = render_manifest(manifest(), context()).workloads[0]
    cluster.rollout_failures.add(
        (context().namespace, workload["kind"], workload["name"])
    )

    async def run() -> None:
        await runtime.deploy_and_promote(
            object(),  # type: ignore[arg-type]
            deployment=deployment(),
            ctx=context(),
            actor_id="worker-b",
        )

    import asyncio

    with pytest.raises(RolloutFailed):
        asyncio.run(run())

    assert cluster.call_names() == ("apply", "wait_rollout")
    assert [name for name, _ in lifecycle.calls] == ["mark_failed"]
    assert lifecycle.calls[0][1]["stage"] == "rollout"
    assert "route" not in str(lifecycle.calls[0][1].get("error_message", "")).lower()


def test_drain_waits_then_deletes_only_the_old_revision() -> None:
    cluster = FakeCluster()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    runtime = DeploymentRuntime(cluster, sleep=fake_sleep)
    old_revision = "sha256:" + "1" * 64
    new_revision = "sha256:" + "2" * 64

    def labels(revision: str) -> dict[str, str]:
        return {
            MANAGED_LABEL: "true",
            PROJECT_LABEL: PROJECT_ID,
            SERVICE_LABEL: "web",
            REVISION_LABEL: label_value(revision),
        }

    cluster.add_pod(context().namespace, "old-pod", labels(old_revision))
    cluster.add_pod(context().namespace, "new-pod", labels(new_revision))

    import asyncio

    deleted = asyncio.run(
        runtime.drain_revision(
            context().namespace,
            project_id=PROJECT_ID,
            service="web",
            revision=old_revision,
            drain_policy=DrainPolicy(max_request_duration_seconds=30),
        )
    )

    assert deleted == 1
    assert sleeps == [30]
    assert set(cluster.pods) == {(context().namespace, "new-pod")}
    assert cluster.call_names() == ("delete_pods",)
    # The delete carries the revision label, not just service/project.
    assert cluster.calls[0][2][REVISION_LABEL] == label_value(old_revision)
    # The wait lets in-flight requests finish; the delete keeps the same bound
    # so a request that started during the wait is not cut off.
    assert cluster.calls[0][3] == 30


def test_project_teardown_leaves_namespace_quota_and_other_projects() -> None:
    cluster = FakeCluster()
    namespace = context().namespace
    project_labels = {
        MANAGED_LABEL: "true",
        ORG_LABEL: ORG_ID,
        PROJECT_LABEL: PROJECT_ID,
    }

    def obj(kind: str, name: str, labels: dict[str, str]) -> dict:
        return {
            "apiVersion": "v1",
            "kind": kind,
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
        }

    cluster.seed_object(obj("ConfigMap", "shop-web", project_labels))
    cluster.seed_object(
        obj(
            "ConfigMap",
            "other-web",
            {
                MANAGED_LABEL: "true",
                ORG_LABEL: ORG_ID,
                PROJECT_LABEL: str(uuid.uuid4()),
            },
        )
    )
    cluster.seed_object(
        obj("Secret", "unmanaged", {PROJECT_LABEL: PROJECT_ID})
    )
    cluster.seed_object(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": namespace,
                "labels": {MANAGED_LABEL: "true", ORG_LABEL: ORG_ID},
            },
        }
    )

    import asyncio

    deleted = asyncio.run(
        delete_project(
            cluster, namespace, org_id=ORG_ID, project_id=PROJECT_ID
        )
    )

    assert deleted == 1
    names = cluster.object_names()
    assert ("ConfigMap", namespace, "shop-web") not in names
    assert ("ConfigMap", namespace, "other-web") in names
    assert ("Secret", namespace, "unmanaged") in names
    assert ("Namespace", "", namespace) in names
    assert cluster.call_names() == ("delete_managed",)


def test_project_teardown_deletes_the_labelled_secret_but_not_an_unlabelled_one() -> None:
    cluster = FakeCluster()
    namespace = context().namespace
    project = {
        MANAGED_LABEL: "true",
        ORG_LABEL: ORG_ID,
        PROJECT_LABEL: PROJECT_ID,
    }

    import asyncio

    asyncio.run(
        cluster.apply_secret(
            namespace, name="shop-secrets", values={"API_KEY": "v"}, labels=project
        )
    )
    asyncio.run(
        cluster.apply_secret(
            namespace, name="unlabelled-secret", values={"OTHER": "v"}, labels=None
        )
    )

    deleted = asyncio.run(
        delete_project(cluster, namespace, org_id=ORG_ID, project_id=PROJECT_ID)
    )

    assert deleted == 1
    assert (namespace, "shop-secrets") not in cluster.secret_values
    assert (namespace, "unlabelled-secret") in cluster.secret_values


def test_publish_secret_only_uses_the_adapter() -> None:
    cluster = FakeCluster()
    runtime = DeploymentRuntime(cluster)

    import asyncio

    asyncio.run(runtime.publish_secret(context(), {"API_KEY": "value-only-in-secret"}))

    assert cluster.secret_values[(context().namespace, "shop-secrets")] == {
        "API_KEY": "value-only-in-secret"
    }
    assert cluster.secret_labels[(context().namespace, "shop-secrets")] == {
        "autopilot/managed": "true",
        "autopilot/org-id": ORG_ID,
        "autopilot/project-id": PROJECT_ID,
        "autopilot/deployment-id": DEPLOYMENT_ID,
        "autopilot/revision": label_value(REVISION),
    }
    assert cluster.call_names() == ("apply_secret",)
    assert "value-only-in-secret" not in render_manifest(manifest(), context()).json()


def test_deploy_publishes_secrets_before_the_workload_apply() -> None:
    events: list[str] = []

    class SecretService:
        async def resolve_for_runtime(self, conn, **kwargs):
            events.append("resolve")
            return {"API_KEY": "rotated-value-never-rendered"}

    cluster = FakeCluster()

    # FakeCluster already has apply; wrap it to preserve call ordering in one
    # list rather than asserting across two independent logs.
    original_apply = cluster.apply
    original_secret = cluster.apply_secret

    async def apply(namespace, objects, *, field_manager="autopilot"):
        events.append("apply")
        await original_apply(namespace, objects, field_manager=field_manager)

    async def apply_secret(namespace, *, name, values, labels=None):
        events.append("apply_secret")
        await original_secret(namespace, name=name, values=values, labels=labels)

    cluster.apply = apply  # type: ignore[method-assign]
    cluster.apply_secret = apply_secret  # type: ignore[method-assign]

    lifecycle = RecordingLifecycle(cluster)
    runtime = DeploymentRuntime(cluster, lifecycle=lifecycle)  # type: ignore[arg-type]

    import asyncio

    async def scenario():
        return await runtime.deploy_and_promote(
            object(),  # type: ignore[arg-type]
            deployment=deployment(),
            ctx=context(),
            actor_id="worker-b",
            secret_service=SecretService(),  # type: ignore[arg-type]
        )

    result = asyncio.run(scenario())

    assert result.routes_updated == 1
    assert events[:3] == ["resolve", "apply_secret", "apply"]
    assert "rotated-value-never-rendered" not in str(cluster.objects)
    assert cluster.secret_values[(context().namespace, "shop-secrets")] == {
        "API_KEY": "rotated-value-never-rendered"
    }
