"""Translate pushes and pull requests into deployment intent (SPEC-08 §3.4).

Ref matching is an ordered list with first-match-wins semantics (R-10). The
store owns the order: `project_branches` has no priority column in SPEC-03, so
the adapter must preserve the order configured by the API (a proposal in
DECISIONS.md).

The default production branch is considered before the catch-all, which is how
"default production is main; the catch-all is preview" is reconciled with an
ordered list. An explicit exact rule still wins over both.
"""

from __future__ import annotations

from buildplane.gitware.models import (
    BranchRule,
    DeploymentIntent,
    Environment,
    Project,
    RouteMatch,
    RouteSource,
    TriggerSource,
)

CATCH_ALL_REF = "*"


def route_push(
    project: Project, rules: list[BranchRule] | tuple[BranchRule, ...], ref: str
) -> RouteMatch | None:
    """Match a head ref against the ordered branch rules.

    Order of evaluation:

    1. exact rules, in the order the store returned them;
    2. the project's default branch -> production (unless an exact rule above
       already decided it);
    3. the first `*` rule -> the catch-all environment (normally preview).

    A tag never matches a branch rule and yields None.
    """

    if not ref.startswith("refs/heads/"):
        return None

    for rule in rules:
        if rule.git_ref == ref:
            return RouteMatch(
                environment=rule.environment,
                auto_deploy=rule.auto_deploy,
                matched_ref=rule.git_ref,
                source=RouteSource.EXACT,
            )

    default_ref = f"refs/heads/{project.default_branch}"
    if ref == default_ref:
        return RouteMatch(
            environment=Environment.PRODUCTION,
            auto_deploy=True,
            matched_ref=default_ref,
            source=RouteSource.DEFAULT,
        )

    for rule in rules:
        if rule.git_ref == CATCH_ALL_REF:
            return RouteMatch(
                environment=rule.environment,
                auto_deploy=rule.auto_deploy,
                matched_ref=rule.git_ref,
                source=RouteSource.CATCH_ALL,
            )
    return None


def build_push_intent(
    project: Project,
    match: RouteMatch,
    *,
    git_sha: str,
    git_ref: str,
    delivery_id: str,
    preview_hostname: str | None = None,
) -> DeploymentIntent:
    """Create the intent for a routable push.

    Branch previews need a hostname; production intents must not carry one.
    """

    if match.environment is Environment.PREVIEW and not preview_hostname:
        raise ValueError("preview push needs a hostname")
    return DeploymentIntent(
        project_id=project.id,
        org_id=project.org_id,
        provider=project.provider,
        provider_repo_id=project.provider_repo_id,
        git_sha=git_sha,
        git_ref=git_ref,
        environment=match.environment,
        trigger=TriggerSource.WEBHOOK,
        delivery_id=delivery_id,
        preview_hostname=preview_hostname if match.environment is Environment.PREVIEW else None,
        matched_ref=match.matched_ref,
    )


def pull_request_ref(number: int) -> str:
    """The stable ref used for a PR's preview deployment history.

    GitHub's `refs/pull/<n>/head` is stable for the life of the PR; each push
    updates it. It is generated from a validated integer, never from a branch
    name supplied in the payload.
    """

    return f"refs/pull/{number}/head"


def build_pull_request_intent(
    project: Project,
    *,
    pr_number: int,
    git_sha: str,
    preview_hostname: str,
    delivery_id: str,
) -> DeploymentIntent:
    return DeploymentIntent(
        project_id=project.id,
        org_id=project.org_id,
        provider=project.provider,
        provider_repo_id=project.provider_repo_id,
        git_sha=git_sha,
        git_ref=pull_request_ref(pr_number),
        environment=Environment.PREVIEW,
        trigger=TriggerSource.WEBHOOK,
        delivery_id=delivery_id,
        pr_number=pr_number,
        preview_hostname=preview_hostname,
    )
