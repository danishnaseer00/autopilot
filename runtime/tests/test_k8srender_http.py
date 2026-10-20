"""SPEC-14 Kubernetes REST adapter tests (R-11, R-12, R-17).

No cluster is contacted: httpx.MockTransport captures the request shapes. These
tests pin server-side apply, the field manager, the scale subresource, and the
fact that teardown never deletes a Namespace.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from runtime.k8srender.cluster import FIELD_MANAGER
from runtime.k8srender.errors import ClusterError
from runtime.k8srender.http import KubernetesApiAdapter


def adapter_with(handler) -> tuple[KubernetesApiAdapter, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = httpx.AsyncClient(
        base_url="https://k8s.test",
        transport=httpx.MockTransport(record),
    )
    return KubernetesApiAdapter(client=client), requests


def test_apply_uses_server_side_apply_and_the_autopilot_field_manager() -> None:
    adapter, requests = adapter_with(
        lambda request: httpx.Response(200, json={"ok": True})
    )
    obj = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "shop-web", "namespace": "autopilot-acme"},
        "data": {"k": "v"},
    }

    asyncio.run(adapter.apply("autopilot-acme", [obj]))

    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/api/v1/namespaces/autopilot-acme/configmaps/shop-web"
    assert request.url.params["fieldManager"] == FIELD_MANAGER
    assert request.headers["content-type"] == "application/apply-patch+yaml"
    assert json.loads(request.content) == obj


def test_namespace_apply_is_cluster_scoped() -> None:
    adapter, requests = adapter_with(
        lambda request: httpx.Response(200, json={"ok": True})
    )
    namespace = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "autopilot-acme"},
    }

    asyncio.run(adapter.apply("autopilot-acme", [namespace]))

    assert requests[0].url.path == "/api/v1/namespaces/autopilot-acme"
    assert "/namespaces/autopilot-acme/namespaces" not in requests[0].url.path


def test_teardown_lists_objects_and_never_deletes_the_namespace() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/deployments"):
            return httpx.Response(
                200,
                json={"items": [{"metadata": {"name": "shop-web"}}]},
            )
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={})

    adapter, requests = adapter_with(handler)

    deleted = asyncio.run(
        adapter.delete_managed(
            "autopilot-acme",
            labels={
                "autopilot/managed": "true",
                "autopilot/org-id": "11111111-1111-1111-1111-111111111111",
                "autopilot/project-id": "22222222-2222-2222-2222-222222222222",
            },
        )
    )

    assert deleted == 1
    deletes = [r for r in requests if r.method == "DELETE"]
    assert len(deletes) == 1
    assert deletes[0].url.path.endswith("/deployments/shop-web")
    assert not any(
        r.url.path == "/api/v1/namespaces/autopilot-acme" for r in deletes
    )
    # No request targets a Namespace resource.
    assert not any("/namespaces/autopilot-acme" == r.url.path for r in requests if r.method == "DELETE")


def test_teardown_requires_a_project_scope() -> None:
    adapter, requests = adapter_with(
        lambda request: httpx.Response(200, json={"items": []})
    )

    for labels in (
        {"autopilot/managed": "true"},
        {
            "autopilot/managed": "true",
            "autopilot/org-id": "11111111-1111-1111-1111-111111111111",
        },
    ):
        with pytest.raises(ClusterError, match="project label"):
            asyncio.run(adapter.delete_managed("autopilot-acme", labels=labels))

    with pytest.raises(ClusterError, match="managed=true"):
        asyncio.run(
            adapter.delete_managed(
                "autopilot-acme",
                labels={
                    "autopilot/managed": "false",
                    "autopilot/project-id": (
                        "22222222-2222-2222-2222-222222222222"
                    ),
                },
            )
        )

    assert requests == []


def test_scale_uses_the_scale_subresource_merge_patch() -> None:
    adapter, requests = adapter_with(
        lambda request: httpx.Response(200, json={"ok": True})
    )

    asyncio.run(
        adapter.scale(
            "autopilot-acme",
            kind="Deployment",
            name="shop-web",
            replicas=3,
        )
    )

    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == (
        "/apis/apps/v1/namespaces/autopilot-acme/deployments/shop-web/scale"
    )
    assert request.headers["content-type"] == "application/merge-patch+json"
    assert json.loads(request.content) == {"spec": {"replicas": 3}}


def test_wait_rollout_accepts_a_ready_deployment() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "metadata": {"name": "shop-web", "generation": 2},
                "spec": {"replicas": 2},
                "status": {
                    "observedGeneration": 2,
                    "readyReplicas": 2,
                    "updatedReplicas": 2,
                    "availableReplicas": 2,
                },
            },
        )

    adapter, requests = adapter_with(handler)

    asyncio.run(
        adapter.wait_rollout(
            "autopilot-acme",
            kind="Deployment",
            name="shop-web",
            timeout_seconds=5,
        )
    )

    assert len(requests) == 1
    assert requests[0].method == "GET"


def test_http_error_message_does_not_echo_the_response_body() -> None:
    adapter, _requests = adapter_with(
        lambda request: httpx.Response(
            422,
            json={"message": "invalid value: supersecret-from-a-manifest"},
        )
    )

    with pytest.raises(ClusterError) as caught:
        asyncio.run(
            adapter.apply(
                "autopilot-acme",
                [
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "shop-web", "namespace": "autopilot-acme"},
                    }
                ],
            )
        )

    assert "supersecret-from-a-manifest" not in str(caught.value)
    assert "422" in str(caught.value)


def test_secret_application_is_the_only_place_values_are_sent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    adapter, requests = adapter_with(handler)

    asyncio.run(
        adapter.apply_secret(
            "autopilot-acme",
            name="shop-secrets",
            values={"API_KEY": "value-only-in-the-secret"},
            labels={
                "autopilot/managed": "true",
                "autopilot/org-id": "11111111-1111-1111-1111-111111111111",
                "autopilot/project-id": "22222222-2222-2222-2222-222222222222",
            },
        )
    )

    request = requests[0]
    assert request.method == "PATCH"
    assert request.url.path == "/api/v1/namespaces/autopilot-acme/secrets/shop-secrets"
    body = json.loads(request.content)
    assert body["stringData"] == {"API_KEY": "value-only-in-the-secret"}
    assert body["metadata"]["name"] == "shop-secrets"
    assert body["metadata"]["labels"]["autopilot/project-id"] == (
        "22222222-2222-2222-2222-222222222222"
    )
