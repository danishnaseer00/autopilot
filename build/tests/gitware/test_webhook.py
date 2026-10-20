"""SPEC-08 acceptance criteria through the webhook service and HTTP route."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from buildplane.gitware import (
    BranchRule,
    DeploymentStatus,
    Environment,
    InMemoryRateLimiter,
    OwnershipVerifier,
    ProjectStatus,
    VerificationMethod,
)
from buildplane.gitware.errors import DeploymentBlocked


def _delivery(handle, signed, service, payload, *, event="push", delivery_id):
    raw, headers = signed(payload, event=event, delivery_id=delivery_id)
    return handle(service, raw, headers)


def test_triple_push_leaves_one_live_deployment_newest_sha(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-6 / R-15: superseding keeps only the newest queued build."""

    store.add_project(make_project())
    service = make_service()
    shas = ["a" * 40, "b" * 40, "c" * 40]

    for index, sha in enumerate(shas):
        outcome = _delivery(
            handle,
            signed,
            service,
            make_push_payload(sha=sha),
            delivery_id=f"triple-{index}",
        )
        assert outcome.status_code == 202

    deployments = store.deployments
    assert [dep.status for dep in deployments] == [
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.SUPERSEDED,
        DeploymentStatus.QUEUED,
    ]
    assert deployments[-1].git_sha == shas[-1]
    assert deployments[0].superseded_by == "dep-0002"
    assert deployments[1].superseded_by == "dep-0003"
    assert len(store.queued("project-1")) == 1

    store.mark_live(deployments[-1].id)
    live = [
        dep
        for dep in store.deployments
        if dep.status is DeploymentStatus.LIVE
    ]
    assert len(live) == 1
    assert live[0].git_sha == shas[-1]


def test_duplicate_delivery_is_idempotent(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """SPEC-02 R-8: GitHub may deliver the same event more than once."""

    store.add_project(make_project())
    service = make_service()
    payload = make_push_payload()
    first = _delivery(handle, signed, service, payload, delivery_id="same")
    second = _delivery(handle, signed, service, payload, delivery_id="same")

    assert first.status_code == second.status_code == 202
    assert second.action == "duplicate"
    assert len(store.deployments) == 1


def test_same_sha_with_a_new_delivery_does_not_build_twice(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(make_project())
    service = make_service()
    payload = make_push_payload()

    first = _delivery(handle, signed, service, payload, delivery_id="d-1")
    second = _delivery(handle, signed, service, payload, delivery_id="d-2")

    assert first.action == "deployment_queued"
    assert second.action == "duplicate_sha"
    assert first.deployment_id == second.deployment_id
    assert len(store.deployments) == 1


def test_same_sha_on_two_prs_gets_two_previews(
    store, make_project, make_service, make_pr_payload, signed, handle
):
    """A commit can head two PRs; each PR owns a distinct preview hostname."""

    store.add_project(make_project())
    service = make_service()
    sha = "d" * 40

    first = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(number=1, sha=sha),
        event="pull_request",
        delivery_id="pr-1",
    )
    second = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(number=2, sha=sha),
        event="pull_request",
        delivery_id="pr-2",
    )

    assert first.action == second.action == "deployment_queued"
    assert first.deployment_id != second.deployment_id
    assert len(store.deployments) == 2
    hostnames = {dep.preview_hostname for dep in store.deployments}
    assert len(hostnames) == 2


def test_unknown_repo_returns_202_no_op(store, make_service, make_push_payload, signed, handle):
    service = make_service()
    raw, headers = signed(
        make_push_payload(repo_id=999999),
        repo_id=999999,
        delivery_id="unknown",
    )
    outcome = handle(service, raw, headers)

    assert outcome.status_code == 202
    assert outcome.action == "unknown_repo"
    assert store.deployments == ()


def test_unverified_project_webhook_records_but_does_not_deploy(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-3 (webhook half): R-8 blocks the deployment, R-7 keeps 202."""

    store.add_project(make_project(verified=False))
    service = make_service()
    outcome = _delivery(
        handle, signed, service, make_push_payload(), delivery_id="unverified"
    )

    assert outcome.status_code == 202
    assert outcome.action == "blocked_unverified"
    assert store.deployments == ()
    assert store.events[-1].outcome == "blocked_unverified"


def test_manual_gate_returns_domain_unverified(
    store, make_project
):
    """AC-3 (console half): SPEC-04's 403 domain_unverified."""

    project = store.add_project(make_project(verified=False))
    verifier = OwnershipVerifier(store)
    with pytest.raises(DeploymentBlocked) as excinfo:
        asyncio.run(verifier.ensure_deployable(project))
    assert excinfo.value.code == "domain_unverified"
    assert excinfo.value.status_code == 403

    asyncio.run(
        store.mark_verified(
            project.id,
            VerificationMethod.TXT,
            at=datetime.now(timezone.utc),
        )
    )
    verified = asyncio.run(store.get_project(project.id))
    assert verified is not None
    asyncio.run(verifier.ensure_deployable(verified))


def test_repository_rename_keeps_routing_by_id(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-7: the name in the payload is irrelevant; the numeric ID routes."""

    project = store.add_project(make_project())
    service = make_service()
    payload = make_push_payload(
        sha="d" * 40, name="totally-new-name"
    )
    outcome = _delivery(handle, signed, service, payload, delivery_id="renamed")

    assert outcome.status_code == 202
    assert outcome.action == "deployment_queued"
    assert store.deployments[0].project_id == project.id
    assert store.deployments[0].git_sha == "d" * 40


def test_unlisted_branch_with_auto_deploy_false_is_202_no_deployment(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-5: a 202 is not a promise that a build was created."""

    store.add_project(
        make_project(),
        rules=(BranchRule("*", Environment.PREVIEW, auto_deploy=False),),
    )
    service = make_service()
    outcome = _delivery(
        handle,
        signed,
        service,
        make_push_payload(ref="refs/heads/feature/unlisted", sha="e" * 40),
        delivery_id="auto-off",
    )

    assert outcome.status_code == 202
    assert outcome.action == "ignored_auto_deploy_off"
    assert store.deployments == ()
    assert store.events[-1].outcome == "ignored_auto_deploy_off"


def test_unlisted_branch_with_auto_deploy_true_gets_a_branch_preview(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(
        make_project(),
        rules=(BranchRule("*", Environment.PREVIEW, auto_deploy=True),),
    )
    service = make_service()
    payload = make_push_payload(ref="refs/heads/feature/unlisted", sha="e" * 40)
    _delivery(handle, signed, service, payload, delivery_id="preview-1")
    _delivery(
        handle,
        signed,
        service,
        make_push_payload(ref="refs/heads/feature/unlisted", sha="f" * 40),
        delivery_id="preview-2",
    )

    deployments = store.deployments
    assert deployments[0].environment is Environment.PREVIEW
    assert deployments[0].preview_hostname == deployments[1].preview_hostname
    assert deployments[0].preview_hostname.startswith(
        "br-demo-app-feature-unlisted-eeeeeeee."
    )
    assert len(store.queued("project-1")) == 1


def test_exact_rule_can_disable_production_auto_deploy(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(
        make_project(),
        rules=(
            BranchRule("refs/heads/main", Environment.PRODUCTION, auto_deploy=False),
        ),
    )
    service = make_service()
    outcome = _delivery(
        handle, signed, service, make_push_payload(), delivery_id="main-off"
    )
    assert outcome.action == "ignored_auto_deploy_off"
    assert store.deployments == ()


def test_two_pr_pushes_reuse_the_same_preview_hostname(
    store, make_project, make_service, make_pr_payload, signed, handle
):
    """AC-4: the hostname is stable per PR even though the SHA changes."""

    store.add_project(make_project())
    service = make_service()

    first = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(number=42, sha="b" * 40),
        event="pull_request",
        delivery_id="pr-1",
    )
    second = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(number=42, sha="c" * 40),
        event="pull_request",
        delivery_id="pr-2",
    )

    assert first.status_code == second.status_code == 202
    deployments = store.deployments
    assert len(deployments) == 2
    assert deployments[0].status is DeploymentStatus.SUPERSEDED
    assert deployments[1].status is DeploymentStatus.QUEUED
    assert deployments[0].preview_hostname == deployments[1].preview_hostname
    assert deployments[0].preview_hostname == (
        "pr-42-demo-app-bbbbbbbb.preview.example.com"
    )
    assert deployments[1].git_sha == "c" * 40


def test_fork_pr_is_allowed_and_uses_the_base_repo(
    store, make_project, make_service, make_pr_payload, signed, handle
):
    store.add_project(make_project())
    service = make_service()
    outcome = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(head_repo_id=999999),
        event="pull_request",
        delivery_id="fork-pr",
    )
    assert outcome.status_code == 202
    assert outcome.action == "deployment_queued"
    assert store.deployments[0].git_ref == "refs/pull/42/head"


def test_deleted_fork_head_is_ignored(
    store, make_project, make_service, make_pr_payload, signed, handle
):
    store.add_project(make_project())
    service = make_service()
    outcome = _delivery(
        handle,
        signed,
        service,
        make_pr_payload(head_repo_id=None),
        event="pull_request",
        delivery_id="deleted-fork",
    )
    assert outcome.status_code == 202
    assert outcome.action == "ignored_head_repo"
    assert store.deployments == ()


def test_repository_410_marks_orphaned_and_stops_builds(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-11 / R-18: keep the data, stop triggering, cancel queued work."""

    project = store.add_project(make_project())
    service = make_service()
    _delivery(handle, signed, service, make_push_payload(), delivery_id="before-410")
    assert len(store.queued(project.id)) == 1

    cancelled = asyncio.run(service.handle_repository_unavailable(project.id))
    assert cancelled == 1

    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.status is ProjectStatus.ORPHANED
    assert store.queued(project.id) == ()

    after = _delivery(
        handle,
        signed,
        service,
        make_push_payload(sha="9" * 40),
        delivery_id="after-410",
    )
    assert after.status_code == 202
    assert after.action == "project_orphaned"
    assert len(store.deployments) == 1
    assert store.deployments[0].status is DeploymentStatus.CANCELLED


def test_repository_deleted_webhook_orphans(
    store, make_project, make_service, make_repository_payload, signed, handle
):
    project = store.add_project(make_project())
    service = make_service()
    outcome = _delivery(
        handle,
        signed,
        service,
        make_repository_payload(action="deleted"),
        event="repository",
        delivery_id="repo-deleted",
    )
    assert outcome.status_code == 202
    assert outcome.action == "orphaned"
    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.status is ProjectStatus.ORPHANED


def test_token_revoked_marks_credential_invalid(
    store, make_project, make_service, make_push_payload, signed, handle
):
    project = store.add_project(make_project())
    service = make_service()
    _delivery(handle, signed, service, make_push_payload(), delivery_id="pre-revoke")
    cancelled = asyncio.run(service.handle_token_revoked(project.id))
    assert cancelled == 1

    updated = asyncio.run(store.get_project(project.id))
    assert updated is not None
    assert updated.status is ProjectStatus.CREDENTIAL_INVALID

    outcome = _delivery(
        handle,
        signed,
        service,
        make_push_payload(sha="8" * 40),
        delivery_id="post-revoke",
    )
    assert outcome.status_code == 202
    assert outcome.action == "project_credential_invalid"
    assert len(store.deployments) == 1


def test_suspended_project_records_event_and_does_not_build(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(make_project(status=ProjectStatus.SUSPENDED))
    service = make_service()
    outcome = _delivery(
        handle, signed, service, make_push_payload(), delivery_id="suspended"
    )
    assert outcome.status_code == 202
    assert outcome.action == "project_suspended"
    assert store.deployments == ()
    assert store.events[-1].outcome == "project_suspended"


def test_rate_limit_counts_without_rejecting(
    store, make_project, make_service, make_push_payload, signed, handle
):
    alerts: list[tuple[str, int]] = []
    limiter = InMemoryRateLimiter(
        limit=2, alert=lambda project_id, count: alerts.append((project_id, count))
    )
    store.add_project(make_project())
    service = make_service(rate_limiter=limiter)

    for index, sha in enumerate(["1" * 40, "2" * 40, "3" * 40, "4" * 40]):
        outcome = _delivery(
            handle,
            signed,
            service,
            make_push_payload(sha=sha),
            delivery_id=f"burst-{index}",
        )
        assert outcome.status_code == 202

    assert limiter.count("project-1") == 4
    assert alerts == [("project-1", 3)]
    assert len(store.deployments) == 4  # superseded, not rejected


def test_ping_and_unsupported_events_are_accepted(
    store, make_project, make_service, signed, handle
):
    store.add_project(make_project())
    service = make_service()

    ping_payload = {"zen": "hello", "repository": {"id": 123456}}
    raw, headers = signed(ping_payload, event="ping", delivery_id="ping-1")
    ping = handle(service, raw, headers)
    assert ping.status_code == 202
    assert ping.action == "pong"

    raw, headers = signed(
        {"repository": {"id": 123456}}, event="issues", delivery_id="issues-1"
    )
    issues = handle(service, raw, headers)
    assert issues.status_code == 202
    assert issues.action == "ignored_event"
    assert store.deployments == ()


def test_metacharacter_sha_is_rejected_end_to_end(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """AC-10: the value never reaches the store or a command."""

    store.add_project(make_project())
    service = make_service()
    outcome = _delivery(
        handle,
        signed,
        service,
        make_push_payload(sha="a" * 39 + ";"),
        delivery_id="injection",
    )
    assert outcome.status_code == 400
    assert outcome.action == "invalid_payload"
    assert store.deployments == ()


def test_event_metadata_never_contains_the_payload(
    store, make_project, make_service, make_push_payload, signed, handle
):
    """R-13: commit messages may contain secrets; keep only routing facts."""

    store.add_project(make_project())
    service = make_service()
    _delivery(handle, signed, service, make_push_payload(), delivery_id="r13")
    record = store.events[-1]
    rendered = repr(record)
    assert "super-secret commit message" not in rendered
    assert "alice@example.com" not in rendered
    assert "commits" not in rendered
    assert record.git_sha == "a" * 40


def test_missing_delivery_or_event_header_is_400(
    store, make_project, make_service, make_push_payload, signed, handle
):
    store.add_project(make_project())
    service = make_service()
    raw, headers = signed(make_push_payload())
    without_delivery = {k: v for k, v in headers.items() if k != "X-GitHub-Delivery"}
    assert handle(service, raw, without_delivery).status_code == 400

    without_event = {k: v for k, v in headers.items() if k != "X-GitHub-Event"}
    assert handle(service, raw, without_event).status_code == 400
    assert store.deployments == ()


def test_handler_returns_under_one_second_with_a_full_queue(
    store, make_project, make_service, make_push_payload, signed, handle, make_client
):
    """AC-9: the handler records intent and returns; it never builds inline."""

    store.add_project(
        make_project(),
        rules=(BranchRule("*", Environment.PREVIEW, auto_deploy=True),),
    )
    service = make_service()
    for index in range(200):
        payload = make_push_payload(
            ref=f"refs/heads/backlog-{index}", sha=f"{index + 1:040x}"
        )
        _delivery(
            handle, signed, service, payload, delivery_id=f"backlog-{index}"
        )
    assert len(store.queued("project-1")) == 200

    client = make_client(service)
    payload = make_push_payload(ref="refs/heads/measured", sha="f" * 40)
    raw, headers = signed(payload, delivery_id="measured")
    started = time.perf_counter()
    response = client.post("/webhooks/github", content=raw, headers=headers)
    elapsed = time.perf_counter() - started

    assert response.status_code == 202
    assert elapsed < 1.0


def test_api_repo_id_hint_selects_the_key_before_body_use(
    store, make_project, make_service, make_push_payload, signed, make_client
):
    store.add_project(make_project())
    service = make_service()
    client = make_client(service)
    raw, headers = signed(make_push_payload(), delivery_id="hinted")

    response = client.post(
        "/webhooks/github?repo_id=123456", content=raw, headers=headers
    )
    assert response.status_code == 202

    mismatch = client.post(
        "/webhooks/github?repo_id=999999", content=raw, headers=headers
    )
    assert mismatch.status_code == 400
    assert len(store.deployments) == 1


def test_api_rejects_oversized_body(
    store, make_project, make_service, signed, make_client, make_push_payload
):
    store.add_project(make_project())
    service = make_service(max_body_bytes=64)
    client = make_client(service)
    raw, headers = signed(make_push_payload(), delivery_id="too-big")
    response = client.post("/webhooks/github", content=raw, headers=headers)
    assert response.status_code == 413
    assert store.deployments == ()
