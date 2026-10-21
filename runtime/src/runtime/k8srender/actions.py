"""SPEC-14 §5: the narrow AutoPilot recovery interface (R-20..R-23).

Three actions, no fourth, no arbitrary patch, no kubeconfig:

* ``restart_service`` - delete the current revision's pods;
* ``rollback`` - re-apply the retained revision, wait for rollout, then cut its
  routes through SPEC-13's single transaction; no build;
* ``scale`` - patch replicas within the manifest/org ceiling, approval required.

Every accepted action writes an audit row *before* the cluster or lifecycle call.
Rejected actions are audited too, so an attempted arbitrary patch is visible.
If audit is unavailable, nothing executes (fail closed).
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Protocol

import asyncpg

from runtime.k8srender.cluster import ClusterAdapter
from runtime.k8srender.context import RenderContext
from runtime.k8srender.deploy import DeploymentRuntime
from runtime.k8srender.errors import (
    ApprovalRequired,
    AuthenticationFailed,
    InvalidAction,
    ScaleLimitExceeded,
)
from runtime.k8srender.names import (
    DEPLOYMENT_LABEL,
    MANAGED_LABEL,
    PROJECT_LABEL,
    SERVICE_LABEL,
    dns_label,
    object_name,
    validate_uuid,
)
from runtime.lifecycle.models import Deployment, PromotionResult
from runtime.lifecycle.service import DeploymentLifecycle
from runtime.secrets.audit import AuditRecord, AuditSink
from runtime.secrets.errors import AuditWriteFailed

ACTIONS = ("restart_service", "rollback", "scale")
_SCALE_DEFAULT_MAX = 5  # SPEC-20's example max_replicas_per_service
_TOP_LEVEL_KEYS = frozenset({"action", "incident_id", "target", "params"})
_PARAM_KEYS: Mapping[str, frozenset[str]] = {
    "restart_service": frozenset(),
    "rollback": frozenset({"deployment_id"}),
    "scale": frozenset({"replicas"}),
}


# --------------------------------------------------------------------- request


@dataclass(frozen=True)
class ActionRequest:
    action: str
    incident_id: str
    target: str
    params: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, payload: Any) -> "ActionRequest":
        if isinstance(payload, cls):
            # Re-validate even an already-built instance: a caller that
            # constructs ActionRequest directly must not bypass the three-action
            # allow-list (AC-11).
            payload = {
                "action": payload.action,
                "incident_id": payload.incident_id,
                "target": payload.target,
                "params": payload.params,
            }
        if not isinstance(payload, Mapping):
            raise InvalidAction("an action request must be a mapping")

        extra = set(payload) - _TOP_LEVEL_KEYS
        if extra:
            raise InvalidAction(
                "unknown action field(s): " + ", ".join(sorted(map(str, extra)))
            )

        action = payload.get("action")
        if not isinstance(action, str) or action not in ACTIONS:
            raise InvalidAction(
                f"action must be one of {list(ACTIONS)}; got {action!r}"
            )

        incident_id = payload.get("incident_id")
        if (
            not isinstance(incident_id, str)
            or not incident_id.strip()
            or len(incident_id) > 128
            or "\x00" in incident_id
        ):
            raise InvalidAction("incident_id must be a non-empty string (max 128)")

        target = payload.get("target")
        if not isinstance(target, str):
            raise InvalidAction("target must be a service name")
        try:
            target = dns_label(target.strip(), field="target")
        except Exception as exc:
            raise InvalidAction("target must be a valid service name") from exc

        raw_params = payload.get("params", {})
        if not isinstance(raw_params, Mapping):
            raise InvalidAction("params must be a mapping")
        extra_params = set(raw_params) - _PARAM_KEYS[action]
        if extra_params:
            raise InvalidAction(
                f"{action} does not accept parameter(s): "
                + ", ".join(sorted(map(str, extra_params)))
            )

        params: dict[str, Any] = {}
        if action == "scale":
            replicas = raw_params.get("replicas")
            if isinstance(replicas, bool) or not isinstance(replicas, int):
                raise InvalidAction("scale.replicas must be an integer")
            if replicas < 1:
                raise InvalidAction("scale.replicas must be at least 1")
            params["replicas"] = replicas
        elif action == "rollback" and "deployment_id" in raw_params:
            try:
                params["deployment_id"] = validate_uuid(
                    raw_params["deployment_id"], "deployment_id"
                )
            except Exception as exc:
                raise InvalidAction("rollback.deployment_id must be a UUID") from exc

        return cls(
            action=action,
            incident_id=incident_id.strip(),
            target=target,
            params=MappingProxyType(params),
        )

    def canonical_params(self) -> dict[str, Any]:
        return dict(self.params)


# --------------------------------------------------------------- authentication


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str


class AgentAuthenticator(Protocol):
    def authenticate(self, credential: str | None) -> AgentIdentity: ...


class StaticTokenAuthenticator:
    """Internal-token authenticator for the FYP deployment and tests.

    Iterates every token with ``compare_digest`` so a wrong token does not reveal
    which prefix matched. Production should replace this with the platform's
    internal service credential (still the same narrow protocol).
    """

    def __init__(self, tokens: Mapping[str, str]) -> None:
        if not tokens:
            raise ValueError("at least one agent token is required")
        self._tokens = {
            str(token): str(agent_id)
            for token, agent_id in tokens.items()
            if token and agent_id
        }
        if len(self._tokens) != len(tokens):
            raise ValueError("agent tokens and ids must be non-empty")

    def authenticate(self, credential: str | None) -> AgentIdentity:
        if not isinstance(credential, str) or not credential:
            raise AuthenticationFailed("agent credential is required")
        matched: str | None = None
        for token, agent_id in self._tokens.items():
            if hmac.compare_digest(token, credential):
                matched = agent_id
        if matched is None:
            raise AuthenticationFailed("agent credential is not valid")
        return AgentIdentity(agent_id=matched)


# --------------------------------------------------------------------- target


@dataclass(frozen=True)
class ActionTarget:
    """Resolved, tenant-scoped facts for one action.

    The resolver (``load_action_target``) is the only place that reads the
    database; this value is what the action service is allowed to act on.
    """

    org_id: str
    project_id: str
    deployment_id: str
    namespace: str
    service: str
    workload_name: str
    workload_kind: str = "Deployment"
    previous_deployment_id: str | None = None
    manifest_max_replicas: int | None = None
    org_max_replicas: int = _SCALE_DEFAULT_MAX
    # Deployment facts the resolver already read. Rollback needs them to build
    # the retained revision's RenderContext without a second project lookup.
    project_slug: str | None = None
    environment: str = "production"
    plan_limits: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "org_id", validate_uuid(self.org_id, "org_id"))
        object.__setattr__(
            self, "project_id", validate_uuid(self.project_id, "project_id")
        )
        object.__setattr__(
            self, "deployment_id", validate_uuid(self.deployment_id, "deployment_id")
        )
        if self.previous_deployment_id is not None:
            object.__setattr__(
                self,
                "previous_deployment_id",
                validate_uuid(self.previous_deployment_id, "previous_deployment_id"),
            )
        dns_label(self.namespace, field="namespace")
        object.__setattr__(
            self, "service", dns_label(self.service, field="service")
        )
        dns_label(self.workload_name, field="workload_name")
        if self.project_slug is not None:
            # project_token() slugifies and truncates the DB slug, so this is
            # deliberately not a strict DNS-label check. _rollback_context
            # proves the resolved object name still matches the target.
            if not isinstance(self.project_slug, str) or not self.project_slug.strip():
                raise InvalidAction("project_slug must be a non-empty string")
            if "\x00" in self.project_slug:
                raise InvalidAction("project_slug must not contain NUL")
            object.__setattr__(self, "project_slug", self.project_slug.strip())
        if not isinstance(self.environment, str) or not self.environment.strip():
            raise InvalidAction("environment must be a non-empty string")
        if "\x00" in self.environment:
            raise InvalidAction("environment must not contain NUL")
        object.__setattr__(self, "environment", self.environment.strip())
        if not isinstance(self.plan_limits, Mapping):
            raise InvalidAction("plan_limits must be a mapping")
        object.__setattr__(
            self, "plan_limits", MappingProxyType(dict(self.plan_limits))
        )
        if self.workload_kind not in {"Deployment", "StatefulSet"}:
            raise InvalidAction("workload_kind must be Deployment or StatefulSet")
        for field_name in ("manifest_max_replicas", "org_max_replicas"):
            value = getattr(self, field_name)
            if value is None and field_name == "manifest_max_replicas":
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise InvalidAction(f"{field_name} must be a positive integer")

    @property
    def scale_ceiling(self) -> int:
        values = [
            value
            for value in (self.manifest_max_replicas, self.org_max_replicas)
            if value is not None
        ]
        return min(values)


# ------------------------------------------------------------------- approval


@dataclass(frozen=True)
class Approval:
    """An approval bound to one action instance (SPEC-34 R-13)."""

    approval_id: str
    action: str
    incident_id: str
    target: str
    params: Mapping[str, Any]
    approved_by: str
    expires_at: datetime

    def __post_init__(self) -> None:
        for field_name in ("approval_id", "action", "incident_id", "target", "approved_by"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.params, Mapping):
            raise ValueError("approval params must be a mapping")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))
        if not isinstance(self.expires_at, datetime):
            raise ValueError("approval expires_at must be a datetime")

    def matches(self, request: ActionRequest, now: datetime) -> bool:
        if not self.approval_id.strip() or not self.approved_by.strip():
            return False
        if self.action != request.action:
            return False
        if self.incident_id != request.incident_id or self.target != request.target:
            return False
        if dict(self.params) != request.canonical_params():
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            # A naive timestamp is not an unambiguous expiry; fail closed.
            return False
        return expires > now


# ------------------------------------------------------------------- result


@dataclass(frozen=True)
class ActionResult:
    action: str
    executed: bool
    detail: Mapping[str, Any] = field(default_factory=dict)


# -------------------------------------------------------------------- service


class AgentActionService:
    def __init__(
        self,
        *,
        adapter: ClusterAdapter,
        authenticator: AgentAuthenticator,
        audit: AuditSink,
        lifecycle: DeploymentLifecycle | None = None,
        runtime: DeploymentRuntime | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._adapter = adapter
        self._authenticator = authenticator
        self._audit = audit
        self._lifecycle = lifecycle
        self._runtime = runtime
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def execute(
        self,
        conn: asyncpg.Connection | None,
        *,
        credential: str | None,
        request: ActionRequest | Mapping[str, Any],
        target: ActionTarget,
        approval: Approval | None = None,
    ) -> ActionResult:
        identity = self._authenticator.authenticate(credential)

        try:
            req = ActionRequest.from_wire(request)
        except InvalidAction as exc:
            await self._audit_rejection(identity, request, target, str(exc))
            raise

        try:
            self._check_target(req, target)
            deployment_id = self._rollback_deployment(req, target)
            if req.action == "scale":
                self._check_scale(req, target)
                self._check_approval(req, approval)
        except (InvalidAction, ApprovalRequired, ScaleLimitExceeded) as exc:
            await self._audit_rejection(identity, req, target, str(exc))
            raise

        metadata: dict[str, Any] = {
            "incident_id": req.incident_id,
            "action": req.action,
            "params": req.canonical_params(),
            "namespace": target.namespace,
            "deployment_id": target.deployment_id,
            "workload": f"{target.workload_kind}/{target.workload_name}",
            "approval": self._approval_metadata(approval),
        }
        if deployment_id is not None:
            metadata["rollback_deployment_id"] = deployment_id

        # R-22: the row exists before anything is executed. Audit failure is
        # fatal, not best-effort.
        await self._audit_or_fail(
            AuditRecord(
                action=f"agent.action.{req.action}",
                org_id=uuid.UUID(target.org_id),
                actor_type="agent",
                actor_id=identity.agent_id,
                target_type="service",
                target_id=req.target,
                metadata=metadata,
            )
        )

        if req.action == "restart_service":
            deleted = await self._adapter.delete_pods(
                target.namespace,
                labels={
                    MANAGED_LABEL: "true",
                    PROJECT_LABEL: target.project_id,
                    SERVICE_LABEL: req.target,
                    DEPLOYMENT_LABEL: target.deployment_id,
                },
                grace_seconds=0,
            )
            return ActionResult(req.action, True, {"pods_deleted": deleted})

        if req.action == "scale":
            replicas = int(req.params["replicas"])
            await self._adapter.scale(
                target.namespace,
                kind=target.workload_kind,
                name=target.workload_name,
                replicas=replicas,
            )
            return ActionResult(
                req.action,
                True,
                {"replicas": replicas, "ceiling": target.scale_ceiling},
            )

        assert req.action == "rollback" and deployment_id is not None
        if self._lifecycle is None:
            raise InvalidAction("rollback requires the deployment lifecycle service")
        if conn is None:
            raise InvalidAction("rollback requires the caller's tenant transaction")

        # R-19: a route-only move cannot bring back an old revision because
        # R-13 keeps one workload per service. Fetch the retained immutable
        # facts, re-apply and wait, and only then perform the route cutover.
        previous: Deployment = await self._lifecycle.get(
            conn, org_id=target.org_id, deployment_id=deployment_id
        )
        self._validate_retained(previous, target)
        ctx = self._rollback_context(previous, target)
        runtime = self._runtime or DeploymentRuntime(
            self._adapter, lifecycle=self._lifecycle
        )
        rendered = await runtime.apply_revision(previous.manifest, ctx)

        result: PromotionResult = await self._lifecycle.rollback(
            conn,
            org_id=target.org_id,
            deployment_id=deployment_id,
            actor_id=identity.agent_id,
            actor_type="agent",
            hostnames=None,
        )
        return ActionResult(
            req.action,
            result.changed,
            {
                "deployment_id": deployment_id,
                "revision": ctx.revision,
                "reapplied": True,
                "objects_applied": len(rendered.objects),
                "routes_updated": result.routes_updated,
                "superseded": [str(item) for item in result.superseded],
            },
        )

    # ---------------------------------------------------------------- checks

    @staticmethod
    def _check_target(req: ActionRequest, target: ActionTarget) -> None:
        if req.target != target.service:
            raise InvalidAction(
                f"request target {req.target!r} does not match the resolved service"
            )

    @staticmethod
    def _rollback_deployment(
        req: ActionRequest, target: ActionTarget
    ) -> str | None:
        if req.action != "rollback":
            return None
        previous = target.previous_deployment_id
        if previous is None:
            raise InvalidAction("rollback has no retained revision to promote")
        requested = req.params.get("deployment_id")
        if requested is not None and requested != previous:
            # The request may name the retained revision, but it may never
            # choose a different deployment. The resolved target is the
            # authority; otherwise a caller could target an arbitrary row.
            raise InvalidAction(
                "rollback deployment_id does not match the resolved previous revision"
            )
        return previous

    @staticmethod
    def _validate_retained(previous: Deployment, target: ActionTarget) -> None:
        if str(previous.project_id) != target.project_id:
            raise InvalidAction(
                "retained revision is outside the resolved project"
            )
        if str(previous.environment) != target.environment:
            raise InvalidAction(
                "retained revision environment does not match the resolved target"
            )
        if (
            previous.status not in {"ready", "live", "superseded"}
            or not previous.revision
        ):
            raise InvalidAction("retained revision is no longer rollbackable")
        if not isinstance(previous.manifest, Mapping) or not previous.manifest:
            raise InvalidAction("retained revision has no manifest")
        if (
            not isinstance(previous.build_image, str)
            or not previous.build_image.strip()
        ):
            raise InvalidAction("retained revision has no build image")

    @staticmethod
    def _rollback_context(previous: Deployment, target: ActionTarget) -> RenderContext:
        if target.project_slug is None:
            raise InvalidAction(
                "rollback requires the resolved project slug and plan limits"
            )
        assert previous.revision is not None
        ctx = RenderContext(
            org_id=target.org_id,
            project_id=target.project_id,
            deployment_id=str(previous.id),
            revision=str(previous.revision),
            namespace=target.namespace,
            project_slug=target.project_slug,
            environment=str(previous.environment),
            runtime_image=str(previous.build_image),
            plan_limits=target.plan_limits,
        )
        if ctx.object_name(target.service) != target.workload_name:
            raise InvalidAction(
                "retained revision does not resolve to the resolved workload"
            )
        return ctx

    @staticmethod
    def _check_scale(req: ActionRequest, target: ActionTarget) -> None:
        replicas = int(req.params["replicas"])
        ceiling = target.scale_ceiling
        if replicas > ceiling:
            raise ScaleLimitExceeded(
                f"scale to {replicas} exceeds the allowed maximum of {ceiling} "
                "(manifest max and org quota, whichever is lower)"
            )

    def _check_approval(
        self, req: ActionRequest, approval: Approval | None
    ) -> None:
        if approval is None:
            raise ApprovalRequired("scale requires an approval bound to this action")
        if not approval.matches(req, self._clock()):
            raise ApprovalRequired(
                "approval does not match this action or has expired"
            )

    @staticmethod
    def _approval_metadata(approval: Approval | None) -> dict[str, Any] | None:
        if approval is None:
            return None
        return {
            "approval_id": approval.approval_id,
            "approved_by": approval.approved_by,
            "expires_at": approval.expires_at.isoformat(),
        }

    # ----------------------------------------------------------------- audit

    async def _audit_rejection(
        self,
        identity: AgentIdentity,
        request: Any,
        target: ActionTarget | None,
        reason: str,
    ) -> None:
        payload = request if isinstance(request, ActionRequest) else None
        if payload is not None:
            action = payload.action
        elif isinstance(request, Mapping):
            action = str(request.get("action", "unknown"))
        else:
            action = "unknown"
        metadata: dict[str, Any] = {
            "requested_action": action,
            "reason": reason[:500],
        }
        if payload is not None:
            metadata["incident_id"] = payload.incident_id
            metadata["target"] = payload.target
            metadata["params"] = payload.canonical_params()
        org_id = uuid.UUID(target.org_id) if target is not None else None
        await self._audit_or_fail(
            AuditRecord(
                action="agent.action_rejected",
                org_id=org_id,
                actor_type="agent",
                actor_id=identity.agent_id,
                target_type="service",
                target_id=payload.target if payload else None,
                metadata=metadata,
            )
        )

    async def _audit_or_fail(self, record: AuditRecord) -> None:
        try:
            await self._audit.write(record)
        except AuditWriteFailed:
            raise
        except Exception as exc:  # boundary: no driver message or value leaks
            raise AuditWriteFailed(
                f"audit write failed for {record.action}; action refused"
            ) from exc


# ------------------------------------------------------------------ resolver


async def load_action_target(
    conn: asyncpg.Connection,
    *,
    org_id: str | uuid.UUID,
    deployment_id: str | uuid.UUID,
    service: str,
) -> ActionTarget:
    """Resolve tenant-scoped action facts from the database.

    The caller must already have set the tenant context. The query joins the
    deployment, project and org plan; no cross-tenant row can satisfy it.
    """
    org = uuid.UUID(str(org_id))
    deployment = uuid.UUID(str(deployment_id))
    row = await conn.fetchrow(
        "SELECT d.id AS deployment_id, d.project_id, d.git_ref, "
        "       d.environment, d.created_at, d.manifest, "
        "       p.slug AS project_slug, p.k8s_namespace, "
        "       pl.limits AS plan_limits "
        "  FROM deployments d "
        "  JOIN projects p ON p.id = d.project_id AND p.org_id = d.org_id "
        "  LEFT JOIN orgs o ON o.id = d.org_id "
        "  LEFT JOIN subscriptions s ON s.org_id = o.id "
        "  LEFT JOIN plans pl ON pl.id = COALESCE(s.plan_id, o.plan_id) "
        " WHERE d.id = $1 AND d.org_id = $2",
        deployment,
        org,
    )
    if row is None:
        raise InvalidAction("deployment is not visible in the caller's tenant")

    manifest = row_manifest(row)
    service_entry = _manifest_service(manifest, service)
    previous = await conn.fetchval(
        "SELECT id FROM deployments "
        " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
        "   AND environment = $4 AND id <> $5 AND revision IS NOT NULL "
        "   AND status IN ('ready','live','superseded') "
        " ORDER BY created_at DESC LIMIT 1",
        org,
        row["project_id"],
        row["git_ref"],
        row["environment"],
        deployment,
    )
    kind = "StatefulSet" if service_entry.get("type") == "stateful" else "Deployment"
    limits = row["plan_limits"] or {}
    org_max = limits.get("max_replicas_per_service")
    if isinstance(org_max, bool) or not isinstance(org_max, int) or org_max < 1:
        org_max = _SCALE_DEFAULT_MAX
    manifest_max = _manifest_max_replicas(service_entry)
    return ActionTarget(
        org_id=str(org),
        project_id=str(row["project_id"]),
        deployment_id=str(deployment),
        namespace=str(row["k8s_namespace"]),
        service=service,
        workload_name=object_name(
            row["project_slug"], row["project_id"], service
        ),
        workload_kind=kind,
        previous_deployment_id=str(previous) if previous is not None else None,
        manifest_max_replicas=manifest_max,
        org_max_replicas=org_max,
        project_slug=str(row["project_slug"]),
        environment=str(row["environment"]),
        plan_limits=dict(limits),
    )


def row_manifest(row: Any) -> dict[str, Any]:
    import json

    manifest = row["manifest"]
    if isinstance(manifest, str):
        return json.loads(manifest)
    if isinstance(manifest, bytes):
        return json.loads(manifest.decode("utf-8"))
    return dict(manifest or {})


def _manifest_service(manifest: Mapping[str, Any], service: str) -> dict[str, Any]:
    for entry in manifest.get("services", []):
        if entry.get("name") == service:
            return dict(entry)
    raise InvalidAction(f"service {service!r} is not in the deployment manifest")


def _manifest_max_replicas(service: Mapping[str, Any]) -> int | None:
    # SPEC-06's optional per-service maxReplicas is the manifest ceiling. When
    # it is absent, the plan ceiling is the only bound (SPEC-14 R-23).
    value = service.get("maxReplicas")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


__all__ = [
    "ACTIONS",
    "ActionResult",
    "ActionRequest",
    "ActionTarget",
    "AgentActionService",
    "AgentAuthenticator",
    "AgentIdentity",
    "Approval",
    "StaticTokenAuthenticator",
    "load_action_target",
]
