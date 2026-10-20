"""SPEC-14 Kubernetes runtime: pure rendering, cluster adapter, agent actions.

The renderer is intentionally pure (R-9). Cluster mutations sit behind
``ClusterAdapter`` so AC-6..AC-14 can be tested against a fake; the coordinator
runs the isolation and rollout criteria against kind/k3s.
"""

from runtime.k8srender.actions import (
    ACTIONS,
    ActionResult,
    ActionRequest,
    ActionTarget,
    AgentActionService,
    AgentAuthenticator,
    AgentIdentity,
    Approval,
    StaticTokenAuthenticator,
    load_action_target,
)
from runtime.k8srender.cluster import FIELD_MANAGER, ClusterAdapter, FakeCluster
from runtime.k8srender.context import RenderContext
from runtime.k8srender.deploy import DeploymentRuntime, delete_project
from runtime.k8srender.errors import (
    ActionError,
    ApprovalRequired,
    AuthenticationFailed,
    ClusterError,
    InvalidAction,
    InvalidRenderContext,
    RenderError,
    RolloutFailed,
    RolloutTimeout,
    ScaleLimitExceeded,
)
from runtime.k8srender.http import KubernetesApiAdapter
from runtime.k8srender.names import namespace_for_org
from runtime.k8srender.render import Rendered, render_json, render_manifest

__all__ = [
    "ACTIONS",
    "ActionError",
    "ActionResult",
    "ActionRequest",
    "ActionTarget",
    "AgentActionService",
    "AgentAuthenticator",
    "AgentIdentity",
    "Approval",
    "ApprovalRequired",
    "AuthenticationFailed",
    "ClusterAdapter",
    "ClusterError",
    "DeploymentRuntime",
    "FIELD_MANAGER",
    "FakeCluster",
    "InvalidAction",
    "InvalidRenderContext",
    "KubernetesApiAdapter",
    "RenderContext",
    "RenderError",
    "Rendered",
    "RolloutFailed",
    "RolloutTimeout",
    "ScaleLimitExceeded",
    "StaticTokenAuthenticator",
    "delete_project",
    "load_action_target",
    "namespace_for_org",
    "render_json",
    "render_manifest",
]
