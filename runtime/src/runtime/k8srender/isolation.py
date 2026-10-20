"""Namespace isolation objects: quota, limits, NetworkPolicy and RBAC.

SPEC-14 R-1..R-8. These are the controls that make the isolation claim true at
admission/network time rather than by application convention:

* one namespace per org, with ``plane: tenant``;
* ResourceQuota derived from the plan;
* LimitRange defaults for every container;
* default-deny ingress and egress;
* allows for ingress-nginx, DNS, the in-cluster registry and the public
  internet - with cluster-private ranges excluded;
* namespace-scoped RBAC for the platform reconciler only. No tenant kubeconfig
  or tenant RoleBinding is ever produced (R-8, AC-14).

**R-6 finding.** A Kubernetes NetworkPolicy is an additive allow-list. It has no
"deny this peer" rule: a ``to:`` entry with no ports allows all ports to that
peer. The spec's example would therefore do the opposite of its comment. The
safe equivalent implemented here is to allow only DNS/registry/public CIDRs and
to exclude cluster-private CIDRs from the "internet" rule, so every platform
namespace is unreachable by construction. This is logged in DECISIONS.md.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from runtime.k8srender.context import RenderContext
from runtime.k8srender.labels import metadata
from runtime.k8srender.names import PLANE_LABEL
from runtime.k8srender.quota import limit_range_limits, quota_from_plan

NAMESPACE_NAME_QUOTA = "org-quota"
NAMESPACE_NAME_LIMITS = "default-limits"
NAMESPACE_NAME_DENY = "default-deny"
NAMESPACE_NAME_ALLOW_INGRESS = "allow-ingress"
NAMESPACE_NAME_ALLOW_EGRESS = "allow-egress"
NAMESPACE_NAME_RBAC = "autopilot-runtime"

RBAC_RULES: tuple[dict[str, Any], ...] = (
    {
        "apiGroups": [""],
        "resources": ["pods", "pods/log"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": [""],
        "resources": ["pods"],
        "verbs": ["delete"],
    },
    {
        "apiGroups": [""],
        "resources": ["services", "configmaps", "secrets"],
        "verbs": [
            "get",
            "list",
            "watch",
            "create",
            "update",
            "patch",
            "delete",
        ],
    },
    {
        "apiGroups": [""],
        "resources": ["persistentvolumeclaims"],
        "verbs": ["get", "list", "watch"],
    },
    {
        "apiGroups": ["apps"],
        "resources": ["deployments", "statefulsets", "replicasets"],
        "verbs": [
            "get",
            "list",
            "watch",
            "create",
            "update",
            "patch",
            "delete",
        ],
    },
    {
        "apiGroups": ["networking.k8s.io"],
        "resources": ["networkpolicies"],
        "verbs": ["get", "list", "watch"],
    },
)


def render_namespace(ctx: RenderContext) -> dict[str, Any]:
    """The org namespace. It is never a teardown target (R-12)."""
    meta = metadata(ctx, ctx.namespace, org_scoped=True)
    meta.pop("namespace", None)  # Namespace is cluster-scoped
    labels = dict(meta["labels"])  # type: ignore[arg-type]
    labels[PLANE_LABEL] = "tenant"
    meta["labels"] = labels
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": meta}


def render_resource_quota(ctx: RenderContext) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ResourceQuota",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_QUOTA, org_scoped=True
        ),
        "spec": {"hard": quota_from_plan(ctx.plan_limits)},
    }


def render_limit_range(ctx: RenderContext) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "LimitRange",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_LIMITS, org_scoped=True
        ),
        "spec": {"limits": limit_range_limits()},
    }


def render_default_deny(ctx: RenderContext) -> dict[str, Any]:
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_DENY, org_scoped=True
        ),
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress", "Egress"],
        },
    }


def render_allow_ingress(
    ctx: RenderContext, service_ports: Sequence[int]
) -> dict[str, Any]:
    ports = [
        {"protocol": "TCP", "port": int(port)}
        for port in sorted(set(int(port) for port in service_ports))
    ]
    ingress: list[dict[str, Any]] = []
    if ports:
        ingress.append(
            {
                "from": [
                    {
                        "namespaceSelector": {
                            "matchLabels": {
                                "kubernetes.io/metadata.name": ctx.ingress_namespace
                            }
                        },
                        "podSelector": {
                            "matchLabels": dict(ctx.ingress_pod_labels)
                        },
                    }
                ],
                "ports": ports,
            }
        )
        # Multi-service manifests and dependsOn init containers need
        # service-to-service traffic. The namespace is the org boundary, so this
        # does not cross the isolation boundary that AC-1 measures.
        ingress.append(
            {
                "from": [{"podSelector": {}}],
                "ports": ports,
            }
        )
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_ALLOW_INGRESS, org_scoped=True
        ),
        "spec": {
            "podSelector": {},
            "policyTypes": ["Ingress"],
            "ingress": ingress,
        },
    }


def render_allow_egress(ctx: RenderContext) -> dict[str, Any]:
    """DNS, registry, internet, and same-namespace service traffic.

    NetworkPolicy rules are additive. Platform namespaces are unreachable
    because no rule admits them and cluster-private CIDRs are excepted from the
    internet block; there is deliberately no rule selecting
    ``platform_namespace_labels``. Same-namespace traffic stays inside the org
    boundary and is required by multi-service apps and dependsOn init
    containers.
    """
    dns_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
                },
                "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}},
            }
        ],
        "ports": [
            {"protocol": "UDP", "port": 53},
            {"protocol": "TCP", "port": 53},
        ],
    }
    registry_rule = {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {
                        "kubernetes.io/metadata.name": ctx.registry_namespace
                    }
                }
            }
        ],
        "ports": [
            {"protocol": "TCP", "port": 443},
            {"protocol": "TCP", "port": 5000},
        ],
    }
    internet_rule = {
        "to": [
            {
                "ipBlock": {
                    "cidr": "0.0.0.0/0",
                    "except": list(ctx.internet_except),
                }
            }
        ]
    }
    same_namespace_rule = {
        "to": [{"podSelector": {}}],
    }
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_ALLOW_EGRESS, org_scoped=True
        ),
        "spec": {
            "podSelector": {},
            "policyTypes": ["Egress"],
            "egress": [
                dns_rule,
                registry_rule,
                internet_rule,
                same_namespace_rule,
            ],
        },
    }


def _rbac_rules() -> list[dict[str, Any]]:
    return [
        {
            "apiGroups": list(rule["apiGroups"]),
            "resources": list(rule["resources"]),
            "verbs": list(rule["verbs"]),
        }
        for rule in RBAC_RULES
    ]


def _rbac_subject(ctx: RenderContext) -> dict[str, str]:
    """Turn ``system:serviceaccount:ns:name`` into a ServiceAccount subject.

    Any other string is carried as a User subject. In both cases it is a
    platform identity; no tenant user is ever bound into the namespace.
    """
    value = ctx.platform_service_account.strip()
    prefix = "system:serviceaccount:"
    if value.startswith(prefix):
        remainder = value[len(prefix) :]
        namespace, _, name = remainder.partition(":")
        if namespace and name:
            return {"kind": "ServiceAccount", "namespace": namespace, "name": name}
    return {"kind": "User", "name": value}


def render_rbac(ctx: RenderContext) -> list[dict[str, Any]]:
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_RBAC, org_scoped=True
        ),
        "rules": _rbac_rules(),
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": metadata(
            ctx, NAMESPACE_NAME_RBAC, org_scoped=True
        ),
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": NAMESPACE_NAME_RBAC,
        },
        "subjects": [_rbac_subject(ctx)],
    }
    return [role, binding]


def render_isolation(
    ctx: RenderContext, service_ports: Sequence[int]
) -> list[dict[str, Any]]:
    return [
        render_namespace(ctx),
        render_resource_quota(ctx),
        render_limit_range(ctx),
        render_default_deny(ctx),
        render_allow_ingress(ctx, service_ports),
        render_allow_egress(ctx),
        *render_rbac(ctx),
    ]


__all__ = [
    "NAMESPACE_NAME_ALLOW_EGRESS",
    "NAMESPACE_NAME_ALLOW_INGRESS",
    "NAMESPACE_NAME_DENY",
    "NAMESPACE_NAME_LIMITS",
    "NAMESPACE_NAME_QUOTA",
    "NAMESPACE_NAME_RBAC",
    "RBAC_RULES",
    "render_allow_egress",
    "render_allow_ingress",
    "render_default_deny",
    "render_isolation",
    "render_limit_range",
    "render_namespace",
    "render_rbac",
    "render_resource_quota",
]
