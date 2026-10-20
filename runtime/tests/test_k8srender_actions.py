"""SPEC-14 agent action tests (AC-11, AC-12, AC-13).

The narrow interface is tested without HTTP or a cluster: strict parsing rejects
anything outside the three actions, audit is written before the adapter call,
scale needs an exactly-bound unexpired approval, and the ceiling is the lower of
the manifest and org values.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from runtime.k8srender.actions import (
    ActionRequest,
    ActionTarget,
    AgentActionService,
    Approval,
    StaticTokenAuthenticator,
    load_action_target,
)
from runtime.k8srender.cluster import FakeCluster
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
)
from runtime.secrets.audit import InMemoryAuditSink
from runtime.secrets.errors import AuditWriteFailed

ORG_ID = "11111111-1111-1111-1111-111111111111"
PROJECT_ID = "22222222-2222-2222-2222-222222222222"
DEPLOYMENT_ID = "33333333-3333-3333-3333-333333333333"
PREVIOUS_ID = "44444444-4444-4444-4444-444444444444"
NAMESPACE = "autopilot-acme"
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def target(**overrides: object) -> ActionTarget:
    values: dict[str, object] = {
        "org_id": ORG_ID,
        "project_id": PROJECT_ID,
        "deployment_id": DEPLOYMENT_ID,
        "namespace": NAMESPACE,
        "service": "web",
        "workload_name": "shop-web",
        "workload_kind": "Deployment",
        "previous_deployment_id": PREVIOUS_ID,
        "manifest_max_replicas": 5,
        "org_max_replicas": 5,
    }
    values.update(overrides)
    return ActionTarget(**values)  # type: ignore[arg-type]


def request(action: str = "scale", **params: object) -> dict:
    return {
        "action": action,
        "incident_id": "inc-1",
        "target": "web",
        "params": params,
    }


def approval(*, replicas: int = 2, expires_in: int = 3600, **overrides: object) -> Approval:
    values: dict[str, object] = {
        "approval_id": "appr-1",
        "action": "scale",
        "incident_id": "inc-1",
        "target": "web",
        "params": {"replicas": replicas},
        "approved_by": "user-1",
        "expires_at": NOW + timedelta(seconds=expires_in),
    }
    values.update(overrides)
    return Approval(**values)  # type: ignore[arg-type]


class OrderedAudit(InMemoryAuditSink):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self.events = events

    async def write(self, record) -> None:
        self.events.append(("audit", record.action))
        await super().write(record)


class OrderedCluster(FakeCluster):
    def __init__(self, events: list[tuple[str, str]]) -> None:
        super().__init__()
        self.events = events

    async def scale(self, namespace: str, *, kind: str, name: str, replicas: int) -> None:
        self.events.append(("adapter", "scale"))
        await super().scale(namespace, kind=kind, name=name, replicas=replicas)

    async def delete_pods(self, namespace: str, *, labels, grace_seconds: int = 0) -> int:
        self.events.append(("adapter", "delete_pods"))
        return await super().delete_pods(
            namespace, labels=labels, grace_seconds=grace_seconds
        )


class FakeLifecycle:
    def __init__(self, events: list[tuple[str, str]] | None = None) -> None:
        self.events = events if events is not None else []
        self.calls: list[dict] = []

    async def rollback(self, conn, **kwargs):
        self.events.append(("lifecycle", "rollback"))
        self.calls.append(kwargs)
        return type(
            "Result",
            (),
            {"changed": True, "routes_updated": 1, "superseded": ()},
        )()


def service(
    cluster: FakeCluster,
    audit: InMemoryAuditSink,
    *,
    lifecycle: FakeLifecycle | None = None,
) -> AgentActionService:
    return AgentActionService(
        adapter=cluster,
        authenticator=StaticTokenAuthenticator({"token-1": "agent-1"}),
        audit=audit,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )


# --------------------------------------------------------------- AC-11 parser


@pytest.mark.parametrize(
    "payload",
    [
        {**request("scale", replicas=2), "patch": {"replicas": 9}},
        {
            "action": "patch_deployment",
            "incident_id": "inc-1",
            "target": "web",
            "params": {},
        },
        {
            "action": "restart_service",
            "incident_id": "inc-1",
            "target": "web",
            "params": {"namespace": "kube-system"},
        },
        {
            "action": "rollback",
            "incident_id": "inc-1",
            "target": "web",
            "params": {"replicas": 2},
        },
    ],
)
def test_parser_rejects_anything_outside_the_three_actions(payload: dict) -> None:
    with pytest.raises(InvalidAction):
        ActionRequest.from_wire(payload)


def test_unknown_action_is_audited_and_never_reaches_the_adapter() -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    action_service = service(cluster, audit)

    async def scenario():
        await action_service.execute(
            None,
            credential="token-1",
            request={**request(), "action": "exec_shell"},
            target=target(),
        )

    import asyncio

    with pytest.raises(InvalidAction):
        asyncio.run(scenario())

    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]
    assert events == [("audit", "agent.action_rejected")]


def test_a_directly_constructed_action_still_goes_through_the_parser() -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    action_service = service(cluster, audit)
    forged = ActionRequest(
        action="exec_shell",
        incident_id="inc-1",
        target="web",
        params={},
    )

    import asyncio

    with pytest.raises(InvalidAction):
        asyncio.run(
            action_service.execute(
                None,
                credential="token-1",
                request=forged,
                target=target(),
            )
        )

    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]


def test_invalid_credential_fails_before_audit_or_execution() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(AuthenticationFailed):
        asyncio.run(
            action_service.execute(
                None,
                credential="wrong",
                request=request("restart_service"),
                target=target(),
            )
        )

    assert cluster.calls == []
    assert audit.rows == []


# ---------------------------------------------------------- AC-12 approval


def test_scale_without_approval_is_rejected_and_audited() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(ApprovalRequired):
        asyncio.run(
            action_service.execute(
                None,
                credential="token-1",
                request=request("scale", replicas=2),
                target=target(),
            )
        )

    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]
    assert audit.rows[0].metadata["requested_action"] == "scale"


def test_expired_or_misbound_approval_is_rejected() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    for bad_approval in (
        approval(expires_in=-1),
        approval(replicas=3),
        approval(target="api"),
        approval(action="restart_service"),
    ):
        with pytest.raises(ApprovalRequired):
            asyncio.run(
                action_service.execute(
                    None,
                    credential="token-1",
                    request=request("scale", replicas=2),
                    target=target(),
                    approval=bad_approval,
                )
            )

    assert cluster.calls == []
    assert len(audit.rows) == 4
    assert all(row.action == "agent.action_rejected" for row in audit.rows)


def test_scale_audits_before_the_adapter_call_and_uses_the_lower_ceiling() -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    action_service = service(cluster, audit)
    cluster.seed_object(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "shop-web", "namespace": NAMESPACE},
            "spec": {"replicas": 1},
        }
    )

    import asyncio

    result = asyncio.run(
        action_service.execute(
            None,
            credential="token-1",
            request=request("scale", replicas=2),
            target=target(manifest_max_replicas=4, org_max_replicas=2),
            approval=approval(replicas=2),
        )
    )

    assert result.executed is True
    assert result.detail["ceiling"] == 2
    assert events == [("audit", "agent.action.scale"), ("adapter", "scale")]
    assert cluster.calls[0][4] == 2


def test_scale_above_the_manifest_ceiling_is_rejected_even_with_approval() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(ScaleLimitExceeded):
        asyncio.run(
            action_service.execute(
                None,
                credential="token-1",
                request=request("scale", replicas=4),
                target=target(manifest_max_replicas=3, org_max_replicas=10),
                approval=approval(replicas=4),
            )
        )

    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]


def test_scale_above_the_org_ceiling_is_rejected() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(ScaleLimitExceeded):
        asyncio.run(
            action_service.execute(
                None,
                credential="token-1",
                request=request("scale", replicas=3),
                target=target(manifest_max_replicas=10, org_max_replicas=2),
                approval=approval(replicas=3),
            )
        )

    assert cluster.calls == []


def test_audit_failure_aborts_before_the_adapter_call() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink(fail=True)
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(AuditWriteFailed):
        asyncio.run(
            action_service.execute(
                None,
                credential="token-1",
                request=request("scale", replicas=2),
                target=target(),
                approval=approval(),
            )
        )

    assert cluster.calls == []


# ------------------------------------------------- AC-11 restart and rollback


def test_restart_deletes_only_the_current_revision_pods() -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    action_service = service(cluster, audit)
    labels = {
        MANAGED_LABEL: "true",
        PROJECT_LABEL: PROJECT_ID,
        SERVICE_LABEL: "web",
        DEPLOYMENT_LABEL: DEPLOYMENT_ID,
    }
    cluster.add_pod(NAMESPACE, "current", labels)
    cluster.add_pod(
        NAMESPACE,
        "old",
        {
            MANAGED_LABEL: "true",
            PROJECT_LABEL: PROJECT_ID,
            SERVICE_LABEL: "web",
            DEPLOYMENT_LABEL: PREVIOUS_ID,
        },
    )

    import asyncio

    result = asyncio.run(
        action_service.execute(
            None,
            credential="token-1",
            request=request("restart_service"),
            target=target(),
        )
    )

    assert result.detail["pods_deleted"] == 1
    assert set(cluster.pods) == {(NAMESPACE, "old")}
    assert events == [("audit", "agent.action.restart_service"), ("adapter", "delete_pods")]


def test_rollback_delegates_to_lifecycle_and_never_touches_the_cluster() -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    lifecycle = FakeLifecycle(events)
    action_service = service(cluster, audit, lifecycle=lifecycle)

    import asyncio

    result = asyncio.run(
        action_service.execute(
            object(),  # type: ignore[arg-type]
            credential="token-1",
            request=request("rollback"),
            target=target(),
        )
    )

    assert result.executed is True
    assert cluster.calls == []
    assert lifecycle.calls == [
        {
            "org_id": ORG_ID,
            "deployment_id": PREVIOUS_ID,
            "actor_id": "agent-1",
            "actor_type": "agent",
            "hostnames": None,
        }
    ]
    assert events == [("audit", "agent.action.rollback"), ("lifecycle", "rollback")]


def test_rollback_without_a_retained_revision_is_rejected() -> None:
    cluster = FakeCluster()
    audit = InMemoryAuditSink()
    action_service = service(cluster, audit)

    import asyncio

    with pytest.raises(InvalidAction):
        asyncio.run(
            action_service.execute(
                object(),  # type: ignore[arg-type]
                credential="token-1",
                request=request("rollback"),
                target=target(previous_deployment_id=None),
            )
        )

    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]


@pytest.mark.parametrize(
    ("previous", "requested"),
    [(None, DEPLOYMENT_ID), (PREVIOUS_ID, DEPLOYMENT_ID)],
)
def test_rollback_cannot_select_a_deployment_outside_the_resolved_target(
    previous: str | None, requested: str
) -> None:
    events: list[tuple[str, str]] = []
    cluster = OrderedCluster(events)
    audit = OrderedAudit(events)
    lifecycle = FakeLifecycle(events)
    action_service = service(cluster, audit, lifecycle=lifecycle)

    import asyncio

    with pytest.raises(InvalidAction):
        asyncio.run(
            action_service.execute(
                object(),  # type: ignore[arg-type]
                credential="token-1",
                request=request("rollback", deployment_id=requested),
                target=target(previous_deployment_id=previous),
            )
        )

    assert lifecycle.calls == []
    assert cluster.calls == []
    assert audit.actions() == ["agent.action_rejected"]


def test_target_resolver_scopes_the_previous_revision_to_the_environment() -> None:
    class ResolverConnection:
        def __init__(self) -> None:
            self.previous_args: tuple[object, ...] | None = None

        async def fetchrow(self, sql: str, *args: object) -> dict[str, object]:
            assert "d.environment" in sql
            return {
                "deployment_id": uuid.UUID(DEPLOYMENT_ID),
                "project_id": uuid.UUID(PROJECT_ID),
                "git_ref": "refs/heads/main",
                "environment": "production",
                "created_at": NOW,
                "manifest": {
                    "version": 1,
                    "name": "shop",
                    "services": [
                        {
                            "name": "web",
                            "type": "node",
                            "runtime": {"port": 3000},
                        }
                    ],
                },
                "project_slug": "shop",
                "k8s_namespace": NAMESPACE,
                "plan_limits": {"max_replicas_per_service": 7},
            }

        async def fetchval(self, sql: str, *args: object) -> str:
            assert "environment = $4" in sql
            self.previous_args = args
            return PREVIOUS_ID

    connection = ResolverConnection()

    import asyncio

    resolved = asyncio.run(
        load_action_target(
            connection,  # type: ignore[arg-type]
            org_id=ORG_ID,
            deployment_id=DEPLOYMENT_ID,
            service="web",
        )
    )

    assert resolved.previous_deployment_id == PREVIOUS_ID
    assert resolved.org_max_replicas == 7
    assert connection.previous_args == (
        uuid.UUID(ORG_ID),
        uuid.UUID(PROJECT_ID),
        "refs/heads/main",
        "production",
        uuid.UUID(DEPLOYMENT_ID),
    )
