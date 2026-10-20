"""SPEC-08 event parsing, routing and preview hostnames."""

from __future__ import annotations

import asyncio

import pytest

from buildplane.gitware import (
    BranchRule,
    Environment,
    InvalidGitRef,
    InvalidGitSha,
    PreviewHostnameAllocator,
    PushEvent,
    RouteSource,
    UnsupportedEvent,
    WebhookPayloadError,
    branch_hostname_candidate,
    parse_event,
    pull_request_hostname_candidate,
    route_push,
    validate_git_ref,
    validate_git_sha,
)


@pytest.mark.parametrize(
    "value",
    [
        "a" * 39 + ";",
        "$(rm -rf /)" + "a" * 28,
        "a" * 40 + "\n",
        "g" * 40,
        "a" * 39,
        "a" * 41,
        123,
        None,
        True,
    ],
)
def test_git_sha_metacharacters_and_wrong_lengths_are_rejected(value):
    """AC-10: validation happens before the value can reach a command."""

    with pytest.raises(InvalidGitSha):
        validate_git_sha(value)


def test_git_sha_normalises_uppercase_hex():
    assert validate_git_sha("A" * 40) == "a" * 40


@pytest.mark.parametrize(
    "ref",
    [
        "refs/heads/main",
        "refs/pull/42/head",
        "refs/heads/release/2026-10",
    ],
)
def test_valid_refs(ref):
    assert validate_git_ref(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "refs/heads/",
        "refs/heads/../main",
        "refs/heads/a b",
        "refs/heads/a\x00b",
        "refs/heads/a..b",
        "refs/heads/a/",
    ],
)
def test_invalid_refs(ref):
    with pytest.raises(InvalidGitRef):
        validate_git_ref(ref)


def test_tags_are_valid_refs_but_never_deployment_refs(make_project):
    assert validate_git_ref("refs/tags/v1") == "refs/tags/v1"
    assert route_push(make_project(), (), "refs/tags/v1") is None


def test_parse_push_keeps_only_routing_fields(make_push_payload):
    payload = make_push_payload()
    event = parse_event("push", payload)
    assert isinstance(event, PushEvent)
    assert event.repo_id == 123456
    assert event.ref == "refs/heads/main"
    assert event.after == "a" * 40
    assert event.deleted is False
    assert not hasattr(event, "commits")


def test_parse_push_deletion_from_zero_sha(make_push_payload):
    event = parse_event("push", make_push_payload(sha="0" * 40, deleted=True))
    assert event.deleted is True


def test_parse_push_rejects_malformed_body(make_push_payload):
    payload = make_push_payload()
    del payload["repository"]
    with pytest.raises(WebhookPayloadError):
        parse_event("push", payload)


def test_parse_pr_allows_fork_head(make_pr_payload):
    event = parse_event(
        "pull_request", make_pr_payload(head_repo_id=999999)
    )
    assert event.number == 42
    assert event.head_repo_id == 999999
    assert event.head_sha == "b" * 40


def test_parse_pr_deleted_fork_head_is_none(make_pr_payload):
    event = parse_event("pull_request", make_pr_payload(head_repo_id=None))
    assert event.head_repo_id is None


def test_parse_pr_rejects_base_repo_mismatch(make_pr_payload):
    with pytest.raises(WebhookPayloadError):
        parse_event(
            "pull_request",
            make_pr_payload(base_repo_id=999999),
        )


def test_parse_pr_closed_is_still_a_typed_event(make_pr_payload):
    event = parse_event("pull_request", make_pr_payload(action="closed"))
    assert event.action == "closed"


def test_unknown_event_is_unsupported(make_push_payload):
    event = parse_event("issues", make_push_payload())
    assert isinstance(event, UnsupportedEvent)


def test_catch_all_must_target_preview():
    """R-10: a production catch-all would deploy every branch to prod."""

    with pytest.raises(ValueError):
        BranchRule("*", Environment.PRODUCTION, auto_deploy=True)
    assert BranchRule("*", Environment.PREVIEW, auto_deploy=False).git_ref == "*"


def test_route_exact_default_and_catch_all(make_project):
    project = make_project()
    rules = [
        BranchRule("refs/heads/release", Environment.PRODUCTION, auto_deploy=False),
        BranchRule("*", Environment.PREVIEW, auto_deploy=True),
    ]

    main = route_push(project, rules, "refs/heads/main")
    assert main is not None
    assert main.environment is Environment.PRODUCTION
    assert main.source is RouteSource.DEFAULT
    assert main.auto_deploy is True

    release = route_push(project, rules, "refs/heads/release")
    assert release is not None
    assert release.source is RouteSource.EXACT
    assert release.auto_deploy is False

    feature = route_push(project, rules, "refs/heads/feature/x")
    assert feature is not None
    assert feature.environment is Environment.PREVIEW
    assert feature.source is RouteSource.CATCH_ALL

    tag = route_push(project, rules, "refs/tags/v1.0.0")
    assert tag is None


def test_route_without_rules_defaults_main_to_production(make_project):
    project = make_project()
    match = route_push(project, (), "refs/heads/main")
    assert match is not None
    assert match.environment is Environment.PRODUCTION
    assert route_push(project, (), "refs/heads/feature") is None


def test_preview_hostname_is_stable_and_uses_first_sha(
    store, make_project
):
    """AC-4: two pushes to one PR reuse the first hostname."""

    project = store.add_project(make_project())
    allocator = PreviewHostnameAllocator(store, "preview.example.com")

    first = asyncio.run(allocator.for_pull_request(project, 42, "a" * 40))
    second = asyncio.run(allocator.for_pull_request(project, 42, "b" * 40))

    assert first == second
    assert first == "pr-42-demo-app-aaaaaaaa.preview.example.com"


def test_preview_hostname_sanitises_slug_and_limits_label():
    hostname = pull_request_hostname_candidate(
        domain="preview.example.com",
        project_slug="Demo App / Ünicode " + "x" * 100,
        pr_number=7,
        git_sha="c" * 40,
    )
    label = hostname.split(".", 1)[0]
    assert len(label) <= 63
    assert label.startswith("pr-7-")
    assert label.endswith("-cccccccc")
    assert " " not in hostname
    assert hostname.split(".")[0].islower()


def test_branch_preview_hostname_is_stable(store, make_project):
    project = store.add_project(make_project())
    allocator = PreviewHostnameAllocator(store, "preview.example.com")

    first = asyncio.run(allocator.for_branch(project, "refs/heads/feature/x", "a" * 40))
    second = asyncio.run(allocator.for_branch(project, "refs/heads/feature/x", "b" * 40))
    assert first == second
    assert first.startswith("br-demo-app-feature-x-aaaaaaaa.")
    assert branch_hostname_candidate(
        domain="preview.example.com",
        project_slug="demo",
        git_ref="refs/heads/main",
        git_sha="a" * 40,
    ) == "br-demo-main-aaaaaaaa.preview.example.com"


def test_preview_hostname_collision_fails_closed(store, make_project):
    """Two tenants with the same slug/PR/SHA cannot share a routing identity."""

    first = store.add_project(make_project(id="project-1"))
    second = store.add_project(
        make_project(id="project-2", org_id="org-2", provider_repo_id=222222)
    )

    allocator = PreviewHostnameAllocator(store, "preview.example.com")
    asyncio.run(allocator.for_pull_request(first, 42, "a" * 40))
    with pytest.raises(ValueError):
        asyncio.run(allocator.for_pull_request(second, 42, "a" * 40))
