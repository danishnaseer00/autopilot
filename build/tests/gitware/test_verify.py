"""SPEC-08 AC-3 and R-8/R-9: ownership verification and the deploy gate."""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta

import pytest

from buildplane.gitware import (
    DeploymentIntent,
    DeploymentStatus,
    Environment,
    OwnershipVerifier,
    ProjectStatus,
    VerificationMethod,
    VerificationUnavailable,
    new_verification_token,
)
from buildplane.gitware.errors import DeploymentBlocked


class FakeTxtResolver:
    def __init__(self, values: dict[str, list[str]]) -> None:
        self.values = values
        self.calls: list[str] = []

    async def records(self, name: str) -> list[str]:
        self.calls.append(name)
        return self.values.get(name, [])


class FakeRepoReader:
    def __init__(
        self,
        branches: list[str],
        files: dict[tuple[str, str], str] | None = None,
    ) -> None:
        self.branches = branches
        self.files = files or {}

    async def list_branches(self) -> list[str]:
        return self.branches

    async def read_file(self, branch: str, path: str) -> str | None:
        return self.files.get((branch, path))


class FailingTxtResolver:
    async def records(self, name: str) -> list[str]:
        raise OSError("DNS is down")


class FailingRepoReader:
    async def list_branches(self) -> list[str]:
        raise OSError("GitHub is down")

    async def read_file(self, branch: str, path: str) -> str | None:
        raise AssertionError("not reached")


class PartiallyFailingRepoReader:
    """One verification branch is unreadable; another simply lacks the file."""

    async def list_branches(self) -> list[str]:
        return ["autopilot-verify-1", "autopilot-verify-2"]

    async def read_file(self, branch: str, path: str) -> str | None:
        if branch == "autopilot-verify-1":
            raise OSError("transient GitHub error")
        return None


def _intent(project) -> DeploymentIntent:
    return DeploymentIntent(
        project_id=project.id,
        org_id=project.org_id,
        provider=project.provider,
        provider_repo_id=project.provider_repo_id,
        git_sha="a" * 40,
        git_ref="refs/heads/main",
        environment=Environment.PRODUCTION,
    )


def test_unverified_project_is_blocked_with_domain_unverified(
    store, make_project
):
    """AC-3's console path: precisely the SPEC-04 code and status."""

    project = store.add_project(make_project(verified=False))
    verifier = OwnershipVerifier(store)
    with pytest.raises(DeploymentBlocked) as excinfo:
        asyncio.run(verifier.ensure_deployable(project))
    assert excinfo.value.code == "domain_unverified"
    assert excinfo.value.status_code == 403


def test_verified_project_passes_the_gate(store, make_project):
    project = store.add_project(make_project(verified=True))
    asyncio.run(OwnershipVerifier(store).ensure_deployable(project))


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (ProjectStatus.SUSPENDED, "tenant_suspended"),
        (ProjectStatus.ORPHANED, "project_orphaned"),
        (ProjectStatus.CREDENTIAL_INVALID, "credential_invalid"),
    ],
)
def test_non_active_projects_are_blocked(store, make_project, status, code):
    project = store.add_project(make_project(status=status))
    with pytest.raises(DeploymentBlocked) as excinfo:
        asyncio.run(OwnershipVerifier(store).ensure_deployable(project))
    assert excinfo.value.code == code
    assert excinfo.value.status_code == 403


def test_txt_verification_marks_the_project(store, make_project, clock):
    project = store.add_project(make_project(verified=False))
    token = project.verification_token
    resolver = FakeTxtResolver(
        {f"_{token}.connect.yourpaas.com": [token]}
    )
    verifier = OwnershipVerifier(store, now=clock)

    result = asyncio.run(verifier.verify_txt(project, resolver))

    assert result.verified is True
    assert result.method is VerificationMethod.TXT
    assert resolver.calls == [f"_{token}.connect.yourpaas.com"]
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is True
    assert updated.verification_method is VerificationMethod.TXT
    assert updated.verification_checked_at == clock()


def test_txt_verification_tolerates_quotes_and_whitespace(
    store, make_project, clock
):
    project = store.add_project(make_project(verified=False))
    token = project.verification_token
    resolver = FakeTxtResolver(
        {f"_{token}.connect.yourpaas.com": [f'  "{token}"  ']}
    )
    result = asyncio.run(
        OwnershipVerifier(store, now=clock).verify_txt(project, resolver)
    )
    assert result.verified is True


def test_txt_wrong_token_does_not_verify(store, make_project, clock):
    project = store.add_project(make_project(verified=False))
    token = project.verification_token
    resolver = FakeTxtResolver(
        {f"_{token}.connect.yourpaas.com": ["b" * len(token)]}
    )
    result = asyncio.run(
        OwnershipVerifier(store, now=clock).verify_txt(project, resolver)
    )
    assert result.verified is False
    assert result.detail == "txt_record_missing"
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is False


def test_txt_outage_is_not_a_negative_proof(store, make_project):
    project = store.add_project(make_project(verified=True))
    with pytest.raises(VerificationUnavailable):
        asyncio.run(
            OwnershipVerifier(store).verify_txt(project, FailingTxtResolver())
        )


def test_deploy_branch_verification(store, make_project, clock):
    project = store.add_project(make_project(verified=False))
    token = project.verification_token
    reader = FakeRepoReader(
        ["main", "autopilot-verify-1"],
        {("autopilot-verify-1", ".autopilot/verify"): token + "\n"},
    )
    result = asyncio.run(
        OwnershipVerifier(store, now=clock).verify_deploy_branch(project, reader)
    )
    assert result.verified is True
    assert result.method is VerificationMethod.DEPLOY_BRANCH
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is True
    assert updated.verification_method is VerificationMethod.DEPLOY_BRANCH


def test_deploy_branch_wrong_token_fails(store, make_project, clock):
    project = store.add_project(make_project(verified=False))
    reader = FakeRepoReader(
        ["autopilot-verify-1"],
        {("autopilot-verify-1", ".autopilot/verify"): "not-the-token"},
    )
    result = asyncio.run(
        OwnershipVerifier(store, now=clock).verify_deploy_branch(project, reader)
    )
    assert result.verified is False
    assert result.detail == "verification_file_missing"


def test_deploy_branch_without_candidates_fails(store, make_project):
    project = store.add_project(make_project(verified=False))
    reader = FakeRepoReader(["main", "feature"])
    result = asyncio.run(
        OwnershipVerifier(store).verify_deploy_branch(project, reader)
    )
    assert result.verified is False


def test_deploy_branch_outage_is_not_a_negative_proof(store, make_project):
    project = store.add_project(make_project(verified=True))
    with pytest.raises(VerificationUnavailable):
        asyncio.run(
            OwnershipVerifier(store).verify_deploy_branch(
                project, FailingRepoReader()
            )
        )


def test_partial_deploy_branch_outage_is_not_a_negative_proof(
    store, make_project
):
    """A transient read error must not un-verify a healthy project."""

    project = store.add_project(make_project(verified=True))
    with pytest.raises(VerificationUnavailable):
        asyncio.run(
            OwnershipVerifier(store).verify_deploy_branch(
                project, PartiallyFailingRepoReader()
            )
        )
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is True


def test_reverify_failure_unverifies_and_cancels_queued(
    store, make_project, clock
):
    """R-9: losing the proof stops the next build, not just the next check."""

    project = store.add_project(make_project(verified=True))
    asyncio.run(store.record_intent(_intent(project), received_at=clock()))
    assert len(store.queued(project.id)) == 1

    resolver = FakeTxtResolver({})  # the TXT record was removed
    result = asyncio.run(
        OwnershipVerifier(store, now=clock).reverify(
            project, VerificationMethod.TXT, resolver=resolver
        )
    )

    assert result.verified is False
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is False
    assert updated.verification_error == "txt_record_missing"
    assert store.queued(project.id) == ()
    assert store.deployments[0].status is DeploymentStatus.CANCELLED


def test_reverify_outage_keeps_existing_verification(store, make_project):
    project = store.add_project(make_project(verified=True))
    with pytest.raises(VerificationUnavailable):
        asyncio.run(
            OwnershipVerifier(store).reverify(
                project,
                VerificationMethod.TXT,
                resolver=FailingTxtResolver(),
            )
        )
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.verified is True


def test_verification_recheck_is_due_after_a_day(
    store, make_project, clock
):
    project = store.add_project(
        make_project(
            verified=True,
            verification_checked_at=clock() - timedelta(hours=25),
        )
    )
    verifier = OwnershipVerifier(store, now=clock)
    assert verifier.verification_due(project) is True

    recent = store.add_project(
        make_project(
            id="project-2",
            provider_repo_id=222222,
            verified=True,
            verification_checked_at=clock() - timedelta(hours=1),
        )
    )
    assert verifier.verification_due(recent) is False

    unverified = store.add_project(
        make_project(id="project-3", provider_repo_id=333333, verified=False)
    )
    assert verifier.verification_due(unverified) is False


def test_bad_verification_token_fails_closed(store, make_project):
    project = store.add_project(
        make_project(verified=False, verification_token="not a dns label")
    )
    with pytest.raises(VerificationUnavailable):
        OwnershipVerifier.expected_txt_name(project)


def test_new_verification_token_is_dns_safe():
    token = new_verification_token()
    assert re.fullmatch(r"[a-z0-9]{32}", token)
    assert len(f"_{token}.connect.example.com") < 63
