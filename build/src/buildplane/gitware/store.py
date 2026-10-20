"""Persistence port for git integration and its in-memory test adapter.

SPEC-03 does not yet have storage for: project verification state, the
`orphaned`/`credential_invalid` statuses, branch-rule ordering, or a
PR/branch -> hostname lookup. The service therefore talks to this port, and
the Postgres adapter can be added when those schema decisions land. The
in-memory adapter is also the reference semantics for superseding (R-15) and
for R-12's lookup-table rule.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from buildplane.gitware.models import (
    BranchRule,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentStatus,
    Project,
    ProjectStatus,
    RecordResult,
    VerificationMethod,
    WebhookEventRecord,
)

_ACTIVE_DEPLOYMENT_STATUSES = frozenset(
    {
        DeploymentStatus.QUEUED,
        DeploymentStatus.BUILDING,
        DeploymentStatus.READY,
        DeploymentStatus.LIVE,
    }
)


class GitStore(Protocol):
    """Everything the webhook service needs from storage.

    Implementations must make `record_intent`, `claim_delivery` and
    `allocate_preview_hostname` atomic. The in-memory adapter relies on the
    single-threaded event loop; a Postgres adapter uses a transaction plus
    unique constraints.
    """

    async def find_project(
        self, provider: str, provider_repo_id: int
    ) -> Project | None: ...

    async def get_project(self, project_id: str) -> Project | None: ...

    async def list_branch_rules(self, project_id: str) -> Sequence[BranchRule]: ...

    async def allocate_preview_hostname(
        self, project_id: str, key: str, candidate: str
    ) -> str: ...

    async def record_intent(
        self, intent: DeploymentIntent, *, received_at: datetime
    ) -> RecordResult: ...

    async def record_event(self, record: WebhookEventRecord) -> None: ...

    async def set_project_status(
        self, project_id: str, status: ProjectStatus
    ) -> None: ...

    async def mark_verified(
        self, project_id: str, method: VerificationMethod, *, at: datetime
    ) -> None: ...

    async def mark_unverified(
        self, project_id: str, *, reason: str, at: datetime
    ) -> None: ...

    async def cancel_queued(
        self, project_id: str, *, reason: str, at: datetime
    ) -> int: ...

    async def claim_delivery(self, delivery_id: str) -> bool:
        """Return True the first time this delivery is seen (R-8 idempotency)."""

    async def release_delivery(self, delivery_id: str) -> None:
        """Undo a claim after an infrastructure failure so GitHub can retry."""


class InMemoryGitStore:
    """Deterministic adapter used by the SPEC-08 acceptance suite.

    IDs are sequence numbers rather than UUIDs so a failing test says
    `dep-0003`, not a random value. Time is supplied by the caller; the store
    never reads the clock.
    """

    def __init__(self) -> None:
        self._projects: dict[str, Project] = {}
        self._repo_index: dict[tuple[str, int], str] = {}
        self._rules: dict[str, tuple[BranchRule, ...]] = {}
        self._previews: dict[tuple[str, str], str] = {}
        self._hostnames: set[str] = set()
        self._deployments: dict[str, DeploymentRecord] = {}
        self._order: list[str] = []
        self._deliveries: set[str] = set()
        self._events: list[WebhookEventRecord] = []
        self._seq = 0

        self.find_calls = 0
        self.record_intent_calls = 0
        self.claim_calls = 0

    # -- test setup helpers -------------------------------------------------

    def add_project(
        self, project: Project, rules: Sequence[BranchRule] = ()
    ) -> Project:
        key = (project.provider, project.provider_repo_id)
        if key in self._repo_index:
            raise ValueError("provider_repo_id is already connected to a project")
        self._projects[project.id] = project
        self._repo_index[key] = project.id
        self._rules[project.id] = tuple(rules)
        return project

    def set_branch_rules(
        self, project_id: str, rules: Sequence[BranchRule]
    ) -> None:
        self._rules[project_id] = tuple(rules)

    @property
    def deployments(self) -> tuple[DeploymentRecord, ...]:
        return tuple(self._deployments[dep_id] for dep_id in self._order)

    @property
    def events(self) -> tuple[WebhookEventRecord, ...]:
        return tuple(self._events)

    def queued(self, project_id: str) -> tuple[DeploymentRecord, ...]:
        return tuple(
            dep
            for dep in self.deployments
            if dep.project_id == project_id
            and dep.status is DeploymentStatus.QUEUED
        )

    # -- GitStore -----------------------------------------------------------

    async def find_project(
        self, provider: str, provider_repo_id: int
    ) -> Project | None:
        self.find_calls += 1
        project_id = self._repo_index.get((provider, provider_repo_id))
        if project_id is None:
            return None
        return self._projects.get(project_id)

    async def get_project(self, project_id: str) -> Project | None:
        return self._projects.get(project_id)

    async def list_branch_rules(
        self, project_id: str
    ) -> Sequence[BranchRule]:
        return self._rules.get(project_id, ())

    async def allocate_preview_hostname(
        self, project_id: str, key: str, candidate: str
    ) -> str:
        existing = self._previews.get((project_id, key))
        if existing is not None:
            return existing
        if candidate in self._hostnames:
            # A preview hostname is a routing identity. Reusing one across
            # tenants would send one project's traffic to another.
            raise ValueError("preview hostname collision")
        self._previews[(project_id, key)] = candidate
        self._hostnames.add(candidate)
        return candidate

    async def record_intent(
        self, intent: DeploymentIntent, *, received_at: datetime
    ) -> RecordResult:
        self.record_intent_calls += 1

        # SPEC-11 R-7's debounce: the same commit on the same ref in the same
        # environment is one build. The ref matters: the same commit can be the
        # head of two PRs, and each PR owns a distinct preview hostname. This
        # also makes GitHub's at-least-once delivery harmless when a retry
        # carries a new delivery ID.
        for deployment in self._deployments.values():
            if (
                deployment.project_id == intent.project_id
                and deployment.git_sha == intent.git_sha
                and deployment.git_ref == intent.git_ref
                and deployment.environment is intent.environment
                and deployment.status in _ACTIVE_DEPLOYMENT_STATUSES
            ):
                return RecordResult(deployment.id, created=False)

        self._seq += 1
        deployment_id = f"dep-{self._seq:04d}"

        # R-15: a newer push to the same ref/environment supersedes older
        # *queued* work. A build that is already running is left alone.
        superseded: list[str] = []
        for old_id, deployment in tuple(self._deployments.items()):
            if (
                deployment.project_id == intent.project_id
                and deployment.git_ref == intent.git_ref
                and deployment.environment is intent.environment
                and deployment.status is DeploymentStatus.QUEUED
            ):
                self._deployments[old_id] = replace(
                    deployment,
                    status=DeploymentStatus.SUPERSEDED,
                    finished_at=received_at,
                    superseded_by=deployment_id,
                )
                superseded.append(old_id)

        record = DeploymentRecord(
            id=deployment_id,
            project_id=intent.project_id,
            org_id=intent.org_id,
            git_sha=intent.git_sha,
            git_ref=intent.git_ref,
            environment=intent.environment,
            status=DeploymentStatus.QUEUED,
            trigger=intent.trigger,
            preview_hostname=intent.preview_hostname,
            pr_number=intent.pr_number,
            matched_ref=intent.matched_ref,
            delivery_id=intent.delivery_id,
            created_at=received_at,
        )
        self._deployments[deployment_id] = record
        self._order.append(deployment_id)
        return RecordResult(
            deployment_id=deployment_id,
            created=True,
            superseded=tuple(superseded),
        )

    async def record_event(self, record: WebhookEventRecord) -> None:
        self._events.append(record)

    async def set_project_status(
        self, project_id: str, status: ProjectStatus
    ) -> None:
        project = self._projects.get(project_id)
        if project is not None:
            self._projects[project_id] = replace(project, status=status)

    async def mark_verified(
        self, project_id: str, method: VerificationMethod, *, at: datetime
    ) -> None:
        project = self._projects.get(project_id)
        if project is None:
            return
        self._projects[project_id] = replace(
            project,
            verified=True,
            verification_method=method,
            verified_at=at,
            verification_checked_at=at,
            verification_error=None,
        )

    async def mark_unverified(
        self, project_id: str, *, reason: str, at: datetime
    ) -> None:
        project = self._projects.get(project_id)
        if project is None:
            return
        self._projects[project_id] = replace(
            project,
            verified=False,
            verification_checked_at=at,
            verification_error=reason,
        )

    async def cancel_queued(
        self, project_id: str, *, reason: str, at: datetime
    ) -> int:
        count = 0
        for deployment_id, deployment in tuple(self._deployments.items()):
            if (
                deployment.project_id == project_id
                and deployment.status is DeploymentStatus.QUEUED
            ):
                self._deployments[deployment_id] = replace(
                    deployment,
                    status=DeploymentStatus.CANCELLED,
                    finished_at=at,
                )
                count += 1
        return count

    async def claim_delivery(self, delivery_id: str) -> bool:
        self.claim_calls += 1
        if delivery_id in self._deliveries:
            return False
        self._deliveries.add(delivery_id)
        return True

    async def release_delivery(self, delivery_id: str) -> None:
        self._deliveries.discard(delivery_id)

    # -- test-only state transitions ---------------------------------------

    def mark_live(self, deployment_id: str, *, at: datetime | None = None) -> None:
        """Simulate the queue promoting a deployment to live.

        The previous live deployment for the same project/ref/environment is
        superseded, so AC-6 can assert exactly one live deployment.
        """

        target = self._deployments[deployment_id]
        for other_id, other in tuple(self._deployments.items()):
            if (
                other_id != deployment_id
                and other.project_id == target.project_id
                and other.git_ref == target.git_ref
                and other.environment is target.environment
                and other.status is DeploymentStatus.LIVE
            ):
                self._deployments[other_id] = replace(
                    other,
                    status=DeploymentStatus.SUPERSEDED,
                    finished_at=at,
                    superseded_by=deployment_id,
                )
        self._deployments[deployment_id] = replace(target, status=DeploymentStatus.LIVE)
