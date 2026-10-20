"""Apply -> wait -> promote, and drain (SPEC-14 R-13, R-17, R-18).

The order is the whole point of this module. Kubernetes changes are applied
first; the route is updated only after every workload is ready. A timeout raises
``RolloutFailed`` and the caller's route rows are untouched, so the previous
revision keeps serving (AC-9).

R-18 draining is explicit: wait the SPEC-13 ``DrainPolicy`` bound, then delete
only the old revision's pods. Kubernetes' own termination grace period is still
set on the pod spec.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import asyncpg

from runtime.k8srender.cluster import ClusterAdapter
from runtime.k8srender.context import RenderContext
from runtime.k8srender.errors import RenderError, RolloutFailed, RolloutTimeout
from runtime.k8srender.labels import project_labels
from runtime.k8srender.names import (
    MANAGED_LABEL,
    ORG_LABEL,
    PROJECT_LABEL,
    REVISION_LABEL,
    SERVICE_LABEL,
    label_value,
    validate_uuid,
)
from runtime.k8srender.render import Rendered, render_manifest
from runtime.lifecycle.models import Deployment, PromotionResult
from runtime.lifecycle.service import DeploymentLifecycle
from runtime.lifecycle.state import DrainPolicy

if TYPE_CHECKING:
    from runtime.secrets.service import SecretService


async def delete_project(
    adapter: ClusterAdapter,
    namespace: str,
    *,
    org_id: str,
    project_id: str,
) -> int:
    """Delete only this project's labelled objects (SPEC-14 R-12, AC-8).

    The selector includes the project label, so org-scoped scaffold (namespace,
    quota, limit ranges, NetworkPolicies, RBAC) cannot match. The adapter also
    refuses Namespace deletion independently.
    """
    return await adapter.delete_managed(
        namespace,
        labels={
            MANAGED_LABEL: "true",
            ORG_LABEL: validate_uuid(org_id, "org_id"),
            PROJECT_LABEL: validate_uuid(project_id, "project_id"),
        },
    )


class DeploymentRuntime:
    """Cluster side of the SPEC-13 promotion transaction."""

    def __init__(
        self,
        adapter: ClusterAdapter,
        *,
        lifecycle: DeploymentLifecycle | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        rollout_timeout_seconds: int = 300,
    ) -> None:
        if rollout_timeout_seconds < 1:
            raise RenderError("rollout_timeout_seconds must be positive")
        self._adapter = adapter
        self._lifecycle = lifecycle
        self._sleep = sleep or asyncio.sleep
        self._timeout = rollout_timeout_seconds

    @property
    def adapter(self) -> ClusterAdapter:
        return self._adapter

    async def apply_revision(
        self,
        manifest: Any,
        ctx: RenderContext,
        *,
        timeout_seconds: int | None = None,
    ) -> Rendered:
        """Render, server-side apply, then wait for every workload.

        If any wait fails, the exception propagates before any route change.
        """
        rendered = render_manifest(manifest, ctx)
        await self._apply_rendered(rendered, ctx, timeout_seconds=timeout_seconds)
        return rendered

    async def _apply_rendered(
        self,
        rendered: Rendered,
        ctx: RenderContext,
        *,
        timeout_seconds: int | None = None,
    ) -> None:
        await self._adapter.apply(ctx.namespace, rendered.objects)
        timeout = timeout_seconds or self._timeout
        for workload in rendered.workloads:
            await self._adapter.wait_rollout(
                str(workload["namespace"]),
                kind=str(workload["kind"]),
                name=str(workload["name"]),
                timeout_seconds=timeout,
            )

    async def deploy_and_promote(
        self,
        conn: asyncpg.Connection,
        *,
        deployment: Deployment,
        ctx: RenderContext,
        actor_id: str,
        actor_type: str = "system",
        hostnames: tuple[str, ...] | None = None,
        mark_failed: bool = True,
        secret_service: "SecretService | None" = None,
    ) -> PromotionResult:
        """The R-17 sequence: apply, wait, then promote in the caller's transaction.

        When ``secret_service`` is supplied, runtime Secret values are resolved
        and written before the workload apply. That is the only path values take
        to the cluster; the renderer and the workload objects never see them
        (R-14). A Secret failure aborts before any workload changes.
        """
        if self._lifecycle is None:
            raise RenderError("deploy_and_promote needs a DeploymentLifecycle")
        _assert_context_matches(deployment, ctx)

        try:
            rendered = render_manifest(deployment.manifest, ctx)
            if secret_service is not None:
                values = await secret_service.resolve_for_runtime(
                    conn,
                    org_id=deployment.org_id,
                    project_id=deployment.project_id,
                    environment=deployment.environment,
                    deployment_id=deployment.id,
                    actor_id=actor_id,
                    actor_type=actor_type,
                )
                await self.publish_secret(ctx, values)
            await self._apply_rendered(rendered, ctx)
        except Exception as exc:
            if mark_failed:
                await self._mark_failed(conn, deployment, exc)
            if isinstance(exc, RolloutTimeout):
                raise RolloutFailed(
                    f"deployment {deployment.id} did not become ready; "
                    "the route was not promoted"
                ) from exc
            raise

        return await self._lifecycle.promote(
            conn,
            org_id=deployment.org_id,
            deployment_id=deployment.id,
            actor_id=actor_id,
            actor_type=actor_type,
            hostnames=hostnames,
        )

    async def drain_revision(
        self,
        namespace: str,
        *,
        project_id: str,
        service: str,
        revision: str,
        drain_policy: DrainPolicy | None = None,
    ) -> int:
        """Wait the request bound, then terminate only that revision's pods.

        The route has already moved. The wait lets requests in flight at promote
        time finish; the DELETE still carries the same termination grace so a
        request that started during the wait is not cut off. Only the old
        revision label is selected.
        """
        policy = drain_policy or DrainPolicy()
        await self._sleep(policy.grace_seconds)
        labels = {
            MANAGED_LABEL: "true",
            PROJECT_LABEL: validate_uuid(project_id, "project_id"),
            SERVICE_LABEL: label_value(service),
            REVISION_LABEL: label_value(revision),
        }
        return await self._adapter.delete_pods(
            namespace, labels=labels, grace_seconds=policy.grace_seconds
        )

    async def publish_secret(
        self, ctx: RenderContext, values: dict[str, str]
    ) -> None:
        """Write the project Secret; only the adapter sees values (R-14)."""
        await self._adapter.apply_secret(
            ctx.namespace,
            name=ctx.resolved_secret_name,
            values=values,
            labels=project_labels(ctx),
        )

    async def _mark_failed(
        self,
        conn: asyncpg.Connection,
        deployment: Deployment,
        exc: Exception,
    ) -> None:
        assert self._lifecycle is not None
        message = f"rollout failed: {type(exc).__name__}"
        try:
            await self._lifecycle.mark_failed(
                conn,
                org_id=deployment.org_id,
                deployment_id=deployment.id,
                stage="rollout",
                error_message=message,
            )
        except Exception:
            # A failure to record the failure must not hide the rollout error.
            # The caller still sees RolloutFailed and the route is unchanged.
            pass


def _assert_context_matches(deployment: Deployment, ctx: RenderContext) -> None:
    if (
        str(deployment.org_id) != ctx.org_id
        or str(deployment.project_id) != ctx.project_id
        or str(deployment.id) != ctx.deployment_id
        or (
            deployment.revision is not None
            and str(deployment.revision) != ctx.revision
        )
        or str(deployment.environment) != ctx.environment
    ):
        raise RenderError(
            "render context does not match the deployment being promoted"
        )


__all__ = ["DeploymentRuntime", "delete_project"]
