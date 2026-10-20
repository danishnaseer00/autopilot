"""Manifest -> workload objects (SPEC-14 §3, R-13, R-14, R-17, R-18).

The renderer emits one workload per service. A revision is applied to the same
workload (same selector), so Kubernetes performs the rolling update and a failed
rollout leaves the old ReplicaSet serving.

Secrets are references only. ``env`` keys from the manifest become
``valueFrom.secretKeyRef`` entries pointing at the project Secret; the renderer
never receives a value (R-14). The only place values go to the cluster is the
adapter's ``apply_secret`` method, fed by SPEC-18's resolution.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from platform_shared.manifest.models import ManifestDocument, ServiceEntry

from runtime.k8srender.context import RenderContext
from runtime.k8srender.errors import RenderError
from runtime.k8srender.labels import metadata, project_labels, selector_labels
from runtime.k8srender.names import (
    DEPENDS_ON_ANNOTATION,
    DEPLOYMENT_LABEL,
    ENVIRONMENT_ANNOTATION,
    MANAGED_LABEL,
    ORG_LABEL,
    PROJECT_LABEL,
    REVISION_LABEL,
    SECRETS_VERSION_ANNOTATION,
    SERVICE_LABEL,
    label_value,
)

DEFAULT_STATIC_IMAGE = "nginx:1.27-alpine"
_STATIC_CONFIG_MOUNT = "/etc/nginx/conf.d"
_STATEFUL_MOUNT = "/data"
_PROGRESS_DEADLINE_SECONDS = 300  # R-17 wait timeout
_REVISION_HISTORY_LIMIT = 10


def service_ports(manifest: ManifestDocument) -> tuple[int, ...]:
    return tuple(sorted({int(service.runtime.port) for service in manifest.services}))


def referenced_env_keys(manifest: ManifestDocument) -> tuple[str, ...]:
    """Every env key the workload must receive, sorted (never includes values)."""
    keys = {
        key
        for service in manifest.services
        for key in service.env.keys()
    }
    return tuple(sorted(keys))


def _quantity_map(amounts: Any) -> dict[str, str]:
    if amounts is None:
        return {}
    out: dict[str, str] = {}
    if amounts.cpu is not None:
        out["cpu"] = amounts.cpu
    if amounts.memory is not None:
        out["memory"] = amounts.memory
    return out


def _resources(service: ServiceEntry) -> dict[str, Any]:
    resources = service.resources
    if resources is None or resources.requests is None:
        # platform_shared validation already rejects this; keep a second guard
        # so a caller bypassing parse_manifest cannot render an unbounded pod.
        raise RenderError(
            f"service {service.name!r} is missing resources.requests (SPEC-06 R-8)"
        )
    requests = _quantity_map(resources.requests)
    if not requests:
        raise RenderError(
            f"service {service.name!r} has empty resources.requests (SPEC-06 R-8)"
        )
    spec: dict[str, Any] = {"requests": requests}
    limits = _quantity_map(resources.limits)
    if limits:
        spec["limits"] = limits
    return spec


def _env(service: ServiceEntry, ctx: RenderContext) -> list[dict[str, Any]]:
    return [
        {
            "name": key,
            "valueFrom": {
                "secretKeyRef": {
                    "name": ctx.resolved_secret_name,
                    "key": key,
                }
            },
        }
        for key in sorted(service.env)
    ]


def _probe(healthcheck: Any) -> dict[str, Any]:
    return {
        "httpGet": {"path": healthcheck.path, "port": "http"},
        "periodSeconds": int(healthcheck.intervalSeconds),
        "timeoutSeconds": 2,
        "failureThreshold": int(healthcheck.failureThreshold),
    }


def _command(command: Sequence[str] | None) -> dict[str, Any]:
    if not command:
        return {}
    values = list(command)
    result: dict[str, Any] = {"command": [values[0]]}
    if len(values) > 1:
        result["args"] = values[1:]
    return result


def service_image(service: ServiceEntry, ctx: RenderContext) -> str:
    if service.image:
        return service.image
    if service.type == "static":
        # A static service is served by the platform nginx image even if the
        # caller supplied a runtime image for the build-produced services.
        # The ConfigMap is the source of the nginx configuration, not the
        # customer's files.
        return DEFAULT_STATIC_IMAGE
    if ctx.runtime_image:
        return ctx.runtime_image
    raise RenderError(
        f"service {service.name!r} has no image and no build; "
        "set RenderContext.runtime_image to the verified built image"
    )


def _init_containers(
    service: ServiceEntry, by_name: Mapping[str, ServiceEntry], ctx: RenderContext
) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for dependency in sorted(service.dependsOn):
        target = by_name.get(dependency)
        if target is None:
            raise RenderError(
                f"service {service.name!r} dependsOn unknown service {dependency!r}"
            )
        name = f"wait-{dependency}"
        if len(name) > 63:
            name = name[:63].rstrip("-")
        # Kubernetes Service names embed the project token; the manifest name
        # alone is not resolvable (two projects may both have a ``db``).
        service_host = ctx.object_name(dependency)
        containers.append(
            {
                "name": name,
                "image": ctx.init_image,
                "imagePullPolicy": "IfNotPresent",
                "command": [
                    "sh",
                    "-c",
                    f"until nc -z -w 2 {service_host} {int(target.runtime.port)}; "
                    "do sleep 2; done",
                ],
            }
        )
    return containers


def _pod_spec(
    service: ServiceEntry,
    by_name: Mapping[str, ServiceEntry],
    ctx: RenderContext,
    *,
    volume_mounts: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": service.name,
        "image": service_image(service, ctx),
        "imagePullPolicy": "IfNotPresent",
        "ports": [
            {
                "name": "http",
                "containerPort": int(service.runtime.port),
                "protocol": "TCP",
            }
        ],
        "resources": _resources(service),
        "env": _env(service, ctx),
        **_command(service.runtime.command),
    }
    if service.healthcheck is not None:
        container["readinessProbe"] = _probe(service.healthcheck)
        container["livenessProbe"] = _probe(service.healthcheck)
    if volume_mounts:
        container["volumeMounts"] = list(volume_mounts)

    pod_spec: dict[str, Any] = {
        "automountServiceAccountToken": False,
        "terminationGracePeriodSeconds": ctx.drain_grace_seconds,
        "containers": [container],
    }
    init_containers = _init_containers(service, by_name, ctx)
    if init_containers:
        pod_spec["initContainers"] = init_containers
    if ctx.image_pull_secrets:
        pod_spec["imagePullSecrets"] = [
            {"name": name} for name in ctx.image_pull_secrets
        ]
    return pod_spec


def _pod_annotations(service: ServiceEntry, ctx: RenderContext) -> dict[str, str]:
    annotations = {
        ENVIRONMENT_ANNOTATION: ctx.environment,
        SECRETS_VERSION_ANNOTATION: str(ctx.secrets_version),
    }
    if service.dependsOn:
        annotations[DEPENDS_ON_ANNOTATION] = ",".join(sorted(service.dependsOn))
    return annotations


def _template(
    service: ServiceEntry,
    by_name: Mapping[str, ServiceEntry],
    ctx: RenderContext,
    *,
    volume_mounts: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    pod_meta: dict[str, Any] = {
        "labels": project_labels(ctx, service=service.name),
        "annotations": _pod_annotations(service, ctx),
    }
    return {
        "metadata": pod_meta,
        "spec": _pod_spec(service, by_name, ctx, volume_mounts=volume_mounts),
    }


def _workload_name(service: ServiceEntry, ctx: RenderContext) -> str:
    return ctx.object_name(service.name)


def render_service(service: ServiceEntry, ctx: RenderContext) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": metadata(
            ctx, _workload_name(service, ctx), service=service.name
        ),
        "spec": {
            "type": "ClusterIP",
            "selector": selector_labels(ctx, service.name),
            "ports": [
                {
                    "name": "http",
                    "protocol": "TCP",
                    "port": int(service.runtime.port),
                    "targetPort": "http",
                }
            ],
        },
    }


def render_static_configmap(
    service: ServiceEntry, ctx: RenderContext
) -> dict[str, Any]:
    name = ctx.object_name(service.name, "nginx")
    config = (
        "server {\n"
        f"    listen {int(service.runtime.port)};\n"
        "    server_name _;\n"
        "    root /usr/share/nginx/html;\n"
        "    index index.html;\n"
        "    location / {\n"
        "        try_files $uri $uri/ /index.html;\n"
        "    }\n"
        "}\n"
    )
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": metadata(
            ctx, name, service=service.name
        ),
        "data": {"default.conf": config},
    }


def _stateful_volume_claim(service: ServiceEntry, ctx: RenderContext) -> dict[str, Any]:
    volume = service.runtime.persistentVolume
    if volume is None:
        raise RenderError(
            f"stateful service {service.name!r} has no persistentVolume (SPEC-06)"
        )
    labels = {
        MANAGED_LABEL: "true",
        ORG_LABEL: ctx.org_id,
        PROJECT_LABEL: ctx.project_id,
        DEPLOYMENT_LABEL: ctx.deployment_id,
        REVISION_LABEL: label_value(ctx.revision),
        SERVICE_LABEL: service.name,
    }
    spec: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": f"{int(volume.sizeGi)}Gi"}},
    }
    if volume.storageClass is not None:
        spec["storageClassName"] = volume.storageClass
    return {
        "metadata": {"name": "data", "labels": labels},
        "spec": spec,
    }


def render_workload(
    service: ServiceEntry,
    by_name: Mapping[str, ServiceEntry],
    ctx: RenderContext,
) -> dict[str, Any]:
    name = _workload_name(service, ctx)
    labels = selector_labels(ctx, service.name)
    volume_mounts: list[dict[str, Any]] = []
    if service.type == "static":
        volume_mounts.append(
            {
                "name": "nginx-config",
                "mountPath": _STATIC_CONFIG_MOUNT,
                "readOnly": True,
            }
        )
    if service.type == "stateful":
        volume_mounts.append(
            {"name": "data", "mountPath": _STATEFUL_MOUNT}
        )

    template = _template(
        service, by_name, ctx, volume_mounts=volume_mounts
    )
    if service.type == "static":
        template["spec"]["volumes"] = [
            {
                "name": "nginx-config",
                "configMap": {
                    "name": ctx.object_name(service.name, "nginx")
                },
            }
        ]

    common = {
        "apiVersion": "apps/v1",
        "metadata": metadata(ctx, name, service=service.name),
        "spec": {
            "replicas": ctx.replicas,
            "selector": {"matchLabels": labels},
            "template": template,
            "revisionHistoryLimit": _REVISION_HISTORY_LIMIT,
        },
    }

    if service.type == "stateful":
        claim = _stateful_volume_claim(service, ctx)
        return {
            **common,
            "kind": "StatefulSet",
            "spec": {
                **common["spec"],
                "serviceName": name,
                "updateStrategy": {"type": "RollingUpdate"},
                "volumeClaimTemplates": [claim],
            },
        }

    return {
        **common,
        "kind": "Deployment",
        "spec": {
            **common["spec"],
            # Deployment-only: StatefulSet has no progressDeadlineSeconds.
            "progressDeadlineSeconds": _PROGRESS_DEADLINE_SECONDS,
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": 1, "maxUnavailable": 0},
            },
        },
    }


def render_workloads(
    manifest: ManifestDocument, ctx: RenderContext
) -> list[dict[str, Any]]:
    by_name = {service.name: service for service in manifest.services}
    objects: list[dict[str, Any]] = []
    for service in manifest.services:
        if service.type == "static":
            objects.append(render_static_configmap(service, ctx))
        objects.append(render_service(service, ctx))
        objects.append(render_workload(service, by_name, ctx))
    return objects


def workload_refs(
    manifest: ManifestDocument, ctx: RenderContext
) -> tuple[dict[str, str], ...]:
    refs: list[dict[str, str]] = []
    for service in manifest.services:
        refs.append(
            {
                "kind": "StatefulSet" if service.type == "stateful" else "Deployment",
                "namespace": ctx.namespace,
                "name": _workload_name(service, ctx),
            }
        )
    return tuple(refs)


__all__ = [
    "DEFAULT_STATIC_IMAGE",
    "referenced_env_keys",
    "render_service",
    "render_static_configmap",
    "render_workload",
    "render_workloads",
    "service_image",
    "service_ports",
    "workload_refs",
]
