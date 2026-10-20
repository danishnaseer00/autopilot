"""SPEC-14 pure renderer acceptance tests (AC-6, AC-7 structurally, AC-14).

These tests need no cluster and no database. They assert the properties that
must hold before any object reaches the API server: deterministic bytes, label
scopes, Secret references instead of values, quota/limits, NetworkPolicy safety,
and namespace-scoped RBAC only.
"""

from __future__ import annotations

import json
import uuid

import pytest

from runtime.k8srender import (
    InvalidRenderContext,
    RenderContext,
    RenderError,
    render_manifest,
)
from runtime.k8srender.names import label_value, namespace_for_org
from runtime.k8srender.render import secret_keys_for

ORG_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"
DEPLOYMENT_ID = "33333333-3333-3333-3333-333333333333"
REVISION = "sha256:" + "a" * 64
RUNTIME_IMAGE = "registry.platform.internal/autopilot/acme/shop@sha256:" + "b" * 64
SECRET_VALUE = "sup3r-secret-value-never-in-yaml"


def context(**overrides: object) -> RenderContext:
    values: dict[str, object] = {
        "org_id": ORG_ID,
        "project_id": PROJECT_ID,
        "deployment_id": DEPLOYMENT_ID,
        "revision": REVISION,
        "namespace": namespace_for_org("acme"),
        "project_slug": "shop",
        "runtime_image": RUNTIME_IMAGE,
        "plan_limits": {"maxCpu": "2", "maxMemory": "4Gi"},
        "drain_grace_seconds": 30,
    }
    values.update(overrides)
    return RenderContext(**values)  # type: ignore[arg-type]


def manifest() -> dict[str, object]:
    return {
        "version": 1,
        "name": "acme-shop",
        "services": [
            {
                "name": "web",
                "source": "./apps/web",
                "type": "node",
                "build": {"strategy": "auto"},
                "runtime": {"port": 3000, "command": ["npm", "start"]},
                "dependsOn": ["api"],
                "healthcheck": {
                    "path": "/healthz",
                    "intervalSeconds": 10,
                    "failureThreshold": 3,
                },
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                    "limits": {"cpu": "500m", "memory": "512Mi"},
                },
                "env": {
                    "DATABASE_URL": "${DATABASE_URL}",
                    "API_KEY": "${API_KEY}",
                },
            },
            {
                "name": "api",
                "source": "./apps/api",
                "type": "python",
                "build": {"strategy": "buildpack"},
                "runtime": {"port": 8000},
                "dependsOn": ["db"],
                "resources": {
                    "requests": {"cpu": "200m", "memory": "256Mi"}
                },
                "env": {"DATABASE_URL": "${DATABASE_URL}"},
            },
            {
                "name": "db",
                "source": "./db",
                "type": "stateful",
                "image": "postgres:16-alpine",
                "runtime": {
                    "port": 5432,
                    "persistentVolume": {"sizeGi": 10, "storageClass": "fast"},
                },
                "resources": {
                    "requests": {"cpu": "250m", "memory": "512Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            },
            {
                "name": "docs",
                "source": "./docs",
                "type": "static",
                "runtime": {"port": 8080},
                "resources": {
                    "requests": {"cpu": "50m", "memory": "64Mi"}
                },
            },
        ],
    }


def one(rendered, kind: str, name: str) -> dict:
    for obj in rendered.objects:
        if obj["kind"] == kind and obj["metadata"]["name"] == name:
            return dict(obj)
    raise AssertionError(f"missing {kind} {name}")


# ---------------------------------------------------------------- AC-6


def test_rendering_is_byte_identical_and_deterministic() -> None:
    first = render_manifest(manifest(), context())
    second = render_manifest(manifest(), context())

    assert first.json() == second.json()
    assert first.object_names() == second.object_names()
    # No wall clock, random suffix or Secret value can enter the canonical bytes.
    assert SECRET_VALUE not in first.json()


def test_apply_order_puts_namespace_and_isolation_before_workloads() -> None:
    rendered = render_manifest(manifest(), context())
    kinds = [str(obj["kind"]) for obj in rendered.objects]

    assert kinds[0] == "Namespace"
    assert kinds.index("ResourceQuota") < kinds.index("Deployment")
    assert kinds.index("NetworkPolicy") < kinds.index("Deployment")
    assert kinds.index("RoleBinding") < kinds.index("Deployment")
    assert len(rendered.workloads) == 4
    assert [ref["name"] for ref in rendered.workloads] == [
        "shop-api",
        "shop-db",
        "shop-docs",
        "shop-web",
    ]


# ------------------------------------------------------- labels and scopes


def test_org_scaffold_does_not_carry_project_labels() -> None:
    rendered = render_manifest(manifest(), context())
    namespace = one(rendered, "Namespace", "autopilot-acme")
    quota = one(rendered, "ResourceQuota", "org-quota")
    binding = one(rendered, "RoleBinding", "autopilot-runtime")

    for obj in (namespace, quota, binding):
        labels = obj["metadata"]["labels"]
        assert labels["autopilot/managed"] == "true"
        assert labels["autopilot/org-id"] == ORG_ID
        assert "autopilot/project-id" not in labels

    assert namespace["metadata"]["labels"]["plane"] == "tenant"


def test_project_objects_carry_the_full_spec14_label_set() -> None:
    rendered = render_manifest(manifest(), context())
    deployment = one(rendered, "Deployment", "shop-web")
    labels = deployment["metadata"]["labels"]

    assert labels == {
        "autopilot/managed": "true",
        "autopilot/org-id": ORG_ID,
        "autopilot/project-id": PROJECT_ID,
        "autopilot/deployment-id": DEPLOYMENT_ID,
        "autopilot/revision": label_value(REVISION),
        "autopilot/service": "web",
    }
    assert deployment["spec"]["selector"]["matchLabels"] == {
        "autopilot/project-id": PROJECT_ID,
        "autopilot/service": "web",
    }
    assert ":" not in labels["autopilot/revision"]


def test_two_projects_can_use_the_same_service_name() -> None:
    other_project = str(uuid.uuid4())
    other_deployment = str(uuid.uuid4())
    other = context(
        project_id=other_project,
        deployment_id=other_deployment,
        project_slug="other",
    )
    first = render_manifest(manifest(), context())
    second = render_manifest(manifest(), other)

    first_names = {obj["metadata"]["name"] for obj in first.objects}
    second_names = {obj["metadata"]["name"] for obj in second.objects}
    assert "shop-web" in first_names
    assert "other-web" in second_names
    assert "shop-web" not in second_names


def test_namespace_is_per_org() -> None:
    assert namespace_for_org("Acme Corp") == "autopilot-acme-corp"
    assert namespace_for_org(uuid.UUID(ORG_ID)) == "autopilot-111111111111"


# ------------------------------------------------- AC-7: references not values


def test_env_is_secret_refs_and_never_literal_values() -> None:
    rendered = render_manifest(manifest(), context())
    text = rendered.json()
    assert SECRET_VALUE not in text

    web = one(rendered, "Deployment", "shop-web")
    containers = web["spec"]["template"]["spec"]["containers"]
    env = {item["name"]: item for item in containers[0]["env"]}

    assert set(env) == {"API_KEY", "DATABASE_URL"}
    for item in env.values():
        assert "value" not in item
        ref = item["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "shop-secrets"
        assert ref["key"] == item["name"]

    assert secret_keys_for(manifest()) == ("API_KEY", "DATABASE_URL")


def test_pod_spec_has_no_service_account_token_and_uses_the_drain_bound() -> None:
    rendered = render_manifest(manifest(), context())
    pod_spec = one(rendered, "Deployment", "shop-web")["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["terminationGracePeriodSeconds"] == 30


def test_secret_rotation_version_changes_the_pod_template() -> None:
    first = render_manifest(manifest(), context(secrets_version=7))
    rotated = render_manifest(manifest(), context(secrets_version=8))
    assert first.json() != rotated.json()

    template = one(first, "Deployment", "shop-web")["spec"]["template"]
    assert template["metadata"]["annotations"]["autopilot.io/secrets-version"] == "7"
    rotated_template = one(rotated, "Deployment", "shop-web")["spec"]["template"]
    assert (
        rotated_template["metadata"]["annotations"]["autopilot.io/secrets-version"]
        == "8"
    )


# ------------------------------------------------------------- workloads


def test_depends_on_becomes_an_init_container() -> None:
    rendered = render_manifest(manifest(), context())
    web = one(rendered, "Deployment", "shop-web")
    init = web["spec"]["template"]["spec"]["initContainers"]

    assert [item["name"] for item in init] == ["wait-api"]
    assert "shop-api 8000" in init[0]["command"][2]
    assert (
        web["spec"]["template"]["metadata"]["annotations"]["autopilot.io/depends-on"]
        == "api"
    )


def test_stateful_service_gets_a_statefulset_and_pvc() -> None:
    rendered = render_manifest(manifest(), context())
    stateful = one(rendered, "StatefulSet", "shop-db")
    assert stateful["spec"]["serviceName"] == "shop-db"
    # progressDeadlineSeconds is a Deployment-only field; sending it on a
    # StatefulSet is rejected by the API server.
    assert "progressDeadlineSeconds" not in stateful["spec"]
    claim = stateful["spec"]["volumeClaimTemplates"][0]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert claim["spec"]["storageClassName"] == "fast"
    assert stateful["spec"]["template"]["spec"]["containers"][0]["volumeMounts"] == [
        {"name": "data", "mountPath": "/data"}
    ]
    assert stateful["spec"]["template"]["spec"]["containers"][0]["image"] == (
        "postgres:16-alpine"
    )


def test_static_service_gets_a_configmap_mounted_nginx() -> None:
    rendered = render_manifest(manifest(), context())
    config = one(rendered, "ConfigMap", "shop-docs-nginx")
    assert "listen 8080;" in config["data"]["default.conf"]

    deployment = one(rendered, "Deployment", "shop-docs")
    pod_spec = deployment["spec"]["template"]["spec"]
    assert pod_spec["volumes"][0]["configMap"]["name"] == "shop-docs-nginx"
    assert pod_spec["containers"][0]["image"] == "nginx:1.27-alpine"
    assert pod_spec["containers"][0]["volumeMounts"] == [
        {"name": "nginx-config", "mountPath": "/etc/nginx/conf.d", "readOnly": True}
    ]


def test_command_is_split_into_command_and_args() -> None:
    web = one(
        render_manifest(manifest(), context()), "Deployment", "shop-web"
    )
    container = web["spec"]["template"]["spec"]["containers"][0]
    assert container["command"] == ["npm"]
    assert container["args"] == ["start"]


def test_probes_use_the_manifest_healthcheck() -> None:
    container = one(
        render_manifest(manifest(), context()), "Deployment", "shop-web"
    )["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/healthz",
        "port": "http",
    }
    assert container["livenessProbe"]["periodSeconds"] == 10
    assert container["livenessProbe"]["failureThreshold"] == 3


# -------------------------------------------------------------- AC-14 RBAC


def test_rbac_is_namespace_scoped_and_never_creates_tenant_kubeconfig() -> None:
    rendered = render_manifest(manifest(), context())
    role = one(rendered, "Role", "autopilot-runtime")
    binding = one(rendered, "RoleBinding", "autopilot-runtime")

    assert role["kind"] == "Role"
    assert all(obj["kind"] != "ClusterRole" for obj in rendered.objects)
    assert all(obj["kind"] != "ClusterRoleBinding" for obj in rendered.objects)
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "namespace": "autopilot-system",
            "name": "autopilot-reconciler",
        }
    ]
    assert "kubeconfig" not in rendered.json().lower()


# ------------------------------------------------------- quota, limits, net


def test_resource_quota_comes_from_the_plan() -> None:
    quota = one(
        render_manifest(manifest(), context()), "ResourceQuota", "org-quota"
    )["spec"]["hard"]
    assert quota["requests.cpu"] == "2"
    assert quota["requests.memory"] == "4Gi"
    assert quota["limits.cpu"] == "4"
    assert quota["limits.memory"] == "8Gi"
    assert quota["pods"] == "50"
    assert quota["count/jobs.autopilot.io"] == "20"


def test_limit_range_supplies_defaults() -> None:
    limits = one(
        render_manifest(manifest(), context()), "LimitRange", "default-limits"
    )["spec"]["limits"]
    assert limits == [
        {
            "type": "Container",
            "default": {"cpu": "500m", "memory": "512Mi"},
            "defaultRequest": {"cpu": "100m", "memory": "128Mi"},
            "max": {"cpu": "4", "memory": "8Gi"},
        }
    ]


def test_network_policies_default_deny_and_allow_only_expected_peers() -> None:
    rendered = render_manifest(manifest(), context())
    deny = one(rendered, "NetworkPolicy", "default-deny")
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"]
    assert "egress" not in deny["spec"]

    ingress = one(rendered, "NetworkPolicy", "allow-ingress")
    assert ingress["spec"]["ingress"][0]["from"][0]["namespaceSelector"] == {
        "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
    }
    assert {port["port"] for port in ingress["spec"]["ingress"][0]["ports"]} == {
        3000,
        5432,
        8000,
        8080,
    }
    assert ingress["spec"]["ingress"][1]["from"] == [{"podSelector": {}}]
    assert ingress["spec"]["ingress"][1]["ports"] == ingress["spec"]["ingress"][0]["ports"]

    egress = one(rendered, "NetworkPolicy", "allow-egress")
    rules = egress["spec"]["egress"]
    assert rules[0]["ports"] == [
        {"protocol": "UDP", "port": 53},
        {"protocol": "TCP", "port": 53},
    ]
    assert rules[1]["to"][0]["namespaceSelector"]["matchLabels"] == {
        "kubernetes.io/metadata.name": "registry"
    }
    block = rules[2]["to"][0]["ipBlock"]
    assert block["cidr"] == "0.0.0.0/0"
    assert "10.0.0.0/8" in block["except"]
    assert "192.168.0.0/16" in block["except"]
    assert rules[3]["to"] == [{"podSelector": {}}]

    # R-6's "explicit deny" cannot be expressed; the safe equivalent is no
    # allow rule selecting platform namespaces.
    assert "plane" not in json.dumps(egress["spec"])


# ------------------------------------------------------------ invalid input


@pytest.mark.parametrize(
    "bad_manifest, message",
    [
        (
            {
                "version": 1,
                "name": "bad",
                "services": [
                    {
                        "name": "Web",
                        "source": ".",
                        "type": "node",
                        "runtime": {"port": 3000},
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "128Mi"}
                        },
                    }
                ],
            },
            "DNS label",
        ),
        (
            {
                "version": 1,
                "name": "bad",
                "services": [
                    {
                        "name": "web",
                        "source": ".",
                        "type": "node",
                        "runtime": {"port": 3000},
                    }
                ],
            },
            "resources.requests",
        ),
    ],
)
def test_unrenderable_manifests_are_rejected(bad_manifest: dict, message: str) -> None:
    with pytest.raises(RenderError, match=message):
        render_manifest(bad_manifest, context())


def test_invalid_context_namespace_is_rejected() -> None:
    with pytest.raises(InvalidRenderContext, match="namespace"):
        context(namespace="Not A Namespace")
