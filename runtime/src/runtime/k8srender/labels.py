"""Labels and selectors (SPEC-14 R-10, R-12).

Two label scopes are deliberate:

* **Org-scoped scaffold** - Namespace, ResourceQuota, LimitRange, NetworkPolicy,
  Role/RoleBinding. These carry ``autopilot/managed`` and ``autopilot/org-id``
  only. They must survive a *project* deletion; if they carried the project
  label, the safe-teardown selector would delete the org's quota and policies.
* **Project-scoped objects** - workloads, Services, ConfigMaps, Secrets. These
  carry the full R-10 set so a project delete or a drift scan can identify them.

Every managed object says ``autopilot/managed=true``; teardown never guesses by
name. Namespaces are never deleted through this path at all (R-12).
"""

from __future__ import annotations

from typing import Mapping

from runtime.k8srender.context import RenderContext
from runtime.k8srender.names import (
    DEPLOYMENT_LABEL,
    ENVIRONMENT_ANNOTATION,
    MANAGED_LABEL,
    ORG_LABEL,
    PROJECT_LABEL,
    REVISION_LABEL,
    SERVICE_LABEL,
    label_value,
)


def org_labels(ctx: RenderContext) -> dict[str, str]:
    return {MANAGED_LABEL: "true", ORG_LABEL: ctx.org_id}


def project_labels(ctx: RenderContext, *, service: str | None = None) -> dict[str, str]:
    labels = {
        MANAGED_LABEL: "true",
        ORG_LABEL: ctx.org_id,
        PROJECT_LABEL: ctx.project_id,
        DEPLOYMENT_LABEL: ctx.deployment_id,
        REVISION_LABEL: label_value(ctx.revision),
    }
    if service is not None:
        labels[SERVICE_LABEL] = label_value(service)
    return labels


def selector_labels(ctx: RenderContext, service: str) -> dict[str, str]:
    """The stable part of a workload selector: project + service, not revision.

    A new revision is applied to the same workload (R-13), so the selector must
    not include the revision or every deploy would create an orphan workload.
    """
    return {
        PROJECT_LABEL: ctx.project_id,
        SERVICE_LABEL: label_value(service),
    }


def metadata(
    ctx: RenderContext,
    name: str,
    *,
    service: str | None = None,
    org_scoped: bool = False,
    annotations: Mapping[str, str] | None = None,
) -> dict[str, object]:
    labels = org_labels(ctx) if org_scoped else project_labels(ctx, service=service)
    meta: dict[str, object] = {"name": name, "namespace": ctx.namespace, "labels": labels}
    merged_annotations = {ENVIRONMENT_ANNOTATION: ctx.environment}
    if annotations:
        merged_annotations.update(annotations)
    if merged_annotations:
        meta["annotations"] = merged_annotations
    return meta


__all__ = [
    "metadata",
    "org_labels",
    "project_labels",
    "selector_labels",
]
