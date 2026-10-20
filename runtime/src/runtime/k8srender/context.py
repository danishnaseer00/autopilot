"""The pure-render input context (SPEC-14 R-9, R-10, R-17, R-18).

Everything a renderer may know comes from this value. There is deliberately no
cluster handle, clock, random source or database connection here: that is what
makes byte-identical rendering and a meaningful ``manifest_hash`` possible.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from runtime.k8srender.errors import InvalidRenderContext, RenderError
from runtime.k8srender.names import (
    dns_label,
    object_name,
    project_token,
    validate_uuid,
)

# R-5: default-deny still needs DNS, the in-cluster registry, and the public
# internet. Cluster-private ranges are excluded from "internet", which is the
# safe way to keep the platform and other tenants unreachable under additive
# NetworkPolicy semantics (see DECISIONS.md, SPEC-14 R-6 finding).
DEFAULT_INTERNET_EXCEPT: tuple[str, ...] = (
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.168.0.0/16",
)

DEFAULT_INGRESS_NAMESPACE = "ingress-nginx"
DEFAULT_INGRESS_POD_LABELS: Mapping[str, str] = MappingProxyType(
    {"app.kubernetes.io/name": "ingress-nginx"}
)
DEFAULT_REGISTRY_NAMESPACE = "registry"
DEFAULT_PLATFORM_SERVICE_ACCOUNT = (
    "system:serviceaccount:autopilot-system:autopilot-reconciler"
)
# Pinned helper image for dependsOn readiness polling. The deploy recipe may
# override this with a digest; the renderer never invents a tag at runtime.
DEFAULT_INIT_IMAGE = "busybox:1.36.1"


def _checked_dns(value: str, field: str) -> str:
    try:
        return dns_label(value, field=field)
    except RenderError as exc:
        raise InvalidRenderContext(str(exc)) from exc


@dataclass(frozen=True)
class RenderContext:
    """Everything that varies between two renders of the same manifest."""

    org_id: str
    project_id: str
    deployment_id: str
    revision: str
    namespace: str
    project_slug: str | None = None
    environment: str = "production"
    # Full registry reference for build-produced services, e.g.
    # registry.platform.internal/autopilot/acme/shop@sha256:...
    runtime_image: str | None = None
    image_pull_secrets: tuple[str, ...] = ()
    secret_name: str | None = None
    plan_limits: Mapping[str, Any] = field(default_factory=dict)
    replicas: int = 1
    drain_grace_seconds: int = 30
    # SPEC-18 R-11 / SPEC-14 R-16. A changed version is rendered into the pod
    # template, so Kubernetes rolls the workload and new containers resolve the
    # rotated Secret at start-up. The version is metadata only; values still
    # never enter the renderer.
    secrets_version: int = 0
    ingress_namespace: str = DEFAULT_INGRESS_NAMESPACE
    ingress_pod_labels: Mapping[str, str] = field(
        default_factory=lambda: dict(DEFAULT_INGRESS_POD_LABELS)
    )
    registry_namespace: str = DEFAULT_REGISTRY_NAMESPACE
    platform_namespace_labels: Mapping[str, str] = field(
        default_factory=lambda: {"plane": "platform"}
    )
    platform_service_account: str = DEFAULT_PLATFORM_SERVICE_ACCOUNT
    init_image: str = DEFAULT_INIT_IMAGE
    internet_except: tuple[str, ...] = DEFAULT_INTERNET_EXCEPT

    def __post_init__(self) -> None:
        for field_name in ("org_id", "project_id", "deployment_id"):
            object.__setattr__(
                self, field_name, validate_uuid(getattr(self, field_name), field_name)
            )
        if not isinstance(self.revision, str) or not self.revision.strip():
            raise InvalidRenderContext("revision must be a non-empty string")
        if len(self.revision) > 255 or "\x00" in self.revision:
            raise InvalidRenderContext("revision must be at most 255 chars without NUL")
        object.__setattr__(self, "revision", self.revision.strip())

        _checked_dns(self.namespace, "namespace")
        if self.secret_name is not None:
            _checked_dns(self.secret_name, "secret_name")
        if self.project_slug is not None:
            _checked_dns(
                project_token(self.project_slug, self.project_id),
                "project_slug token",
            )
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise InvalidRenderContext("environment must be a non-empty string")
        if "\x00" in self.environment:
            raise InvalidRenderContext("environment must not contain NUL")
        object.__setattr__(self, "environment", self.environment.strip())

        if self.runtime_image is not None:
            if not isinstance(self.runtime_image, str) or not self.runtime_image.strip():
                raise InvalidRenderContext("runtime_image must be a non-empty string")
            if "\x00" in self.runtime_image:
                raise InvalidRenderContext("runtime_image must not contain NUL")

        if isinstance(self.replicas, bool) or not isinstance(self.replicas, int):
            raise InvalidRenderContext("replicas must be an integer")
        if self.replicas < 1:
            raise InvalidRenderContext("replicas must be at least 1")

        if isinstance(self.drain_grace_seconds, bool) or not isinstance(
            self.drain_grace_seconds, int
        ):
            raise InvalidRenderContext("drain_grace_seconds must be an integer")
        if self.drain_grace_seconds < 0:
            raise InvalidRenderContext("drain_grace_seconds must be non-negative")

        if isinstance(self.secrets_version, bool) or not isinstance(
            self.secrets_version, int
        ):
            raise InvalidRenderContext("secrets_version must be an integer")
        if self.secrets_version < 0:
            raise InvalidRenderContext("secrets_version must be non-negative")

        if not isinstance(self.plan_limits, Mapping):
            raise InvalidRenderContext("plan_limits must be a mapping")
        object.__setattr__(
            self, "plan_limits", MappingProxyType(dict(self.plan_limits))
        )
        if not isinstance(self.ingress_pod_labels, Mapping):
            raise InvalidRenderContext("ingress_pod_labels must be a mapping")
        object.__setattr__(
            self,
            "ingress_pod_labels",
            MappingProxyType(dict(self.ingress_pod_labels)),
        )
        if not isinstance(self.platform_namespace_labels, Mapping):
            raise InvalidRenderContext("platform_namespace_labels must be a mapping")
        object.__setattr__(
            self,
            "platform_namespace_labels",
            MappingProxyType(dict(self.platform_namespace_labels)),
        )
        object.__setattr__(
            self,
            "image_pull_secrets",
            tuple(
                _checked_dns(name, "image pull secret")
                for name in self.image_pull_secrets
            ),
        )
        _checked_dns(self.ingress_namespace, "ingress_namespace")
        _checked_dns(self.registry_namespace, "registry_namespace")
        if not isinstance(self.init_image, str) or not self.init_image.strip():
            raise InvalidRenderContext("init_image must be a non-empty string")
        if not isinstance(self.platform_service_account, str) or not self.platform_service_account.strip():
            raise InvalidRenderContext("platform_service_account must be a non-empty string")

        for cidr in self.internet_except:
            try:
                ipaddress.ip_network(str(cidr), strict=False)
            except ValueError as exc:
                raise InvalidRenderContext(
                    f"internet_except contains an invalid CIDR: {cidr!r}"
                ) from exc

    @property
    def token(self) -> str:
        return project_token(self.project_slug, self.project_id)

    @property
    def resolved_secret_name(self) -> str:
        return self.secret_name or f"{self.token}-secrets"

    def object_name(self, service_name: str, *suffixes: str) -> str:
        return object_name(
            self.project_slug, self.project_id, service_name, *suffixes
        )


__all__ = [
    "DEFAULT_INGRESS_NAMESPACE",
    "DEFAULT_INGRESS_POD_LABELS",
    "DEFAULT_INIT_IMAGE",
    "DEFAULT_INTERNET_EXCEPT",
    "DEFAULT_PLATFORM_SERVICE_ACCOUNT",
    "DEFAULT_REGISTRY_NAMESPACE",
    "RenderContext",
]
