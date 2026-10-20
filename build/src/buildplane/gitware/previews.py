"""Preview hostname issuance (SPEC-08 R-11, R-12).

A hostname is allocated once per stable identity and then reused:

* a pull request is `pr-<number>-<repo>-<sha8>.preview.<domain>`, where
  `<sha8>` is the first push's SHA;
* a branch preview is `br-<branch>-<sha8>...` (the spec only defines PR
  hostnames; this is the conservative extension, logged in DECISIONS.md).

Later pushes ask the allocator again. The store returns the existing hostname,
so reviewers' links survive new commits (AC-4). Nothing ever parses a hostname
to find a deployment (R-12); the stable key is stored alongside it.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

from buildplane.gitware.errors import InvalidGitRef
from buildplane.gitware.models import Project
from buildplane.gitware.validation import (
    DNS_LABEL_RE,
    validate_git_ref,
    validate_git_sha,
    validate_pr_number,
    validate_preview_domain,
)

MAX_DNS_LABEL = 63
MAX_HOSTNAME = 253


class PreviewHostnameStore(Protocol):
    async def allocate_preview_hostname(
        self, project_id: str, key: str, candidate: str
    ) -> str:
        """Return the hostname already allocated for `key`, else `candidate`."""


def _sanitise_label(value: str) -> str:
    """Turn a project slug or branch name into one DNS label.

    Unicode is transliterated to ASCII, every run of non-alphanumerics becomes
    a hyphen, and the result is lowercased. Empty results fall back to `app`.
    """

    normalised = unicodedata.normalize("NFKD", value)
    ascii_text = normalised.encode("ascii", "ignore").decode("ascii").lower()
    label = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return label or "app"


def _build_hostname(*, prefix: str, slug: str, sha: str, domain: str) -> str:
    sha8 = validate_git_sha(sha)[:8]
    suffix = f"-{sha8}"
    available = MAX_DNS_LABEL - len(prefix) - len(suffix)
    if available < 1:
        raise ValueError("preview hostname prefix is too long")
    slug_part = _sanitise_label(slug)[:available].rstrip("-")
    if not slug_part:
        slug_part = "app"[:available]
    label = f"{prefix}{slug_part}{suffix}"
    if len(label) > MAX_DNS_LABEL:
        raise ValueError("preview hostname label is too long")
    if DNS_LABEL_RE.fullmatch(label) is None:
        raise ValueError("preview hostname label is not DNS-safe")
    hostname = f"{label}.{domain}"
    if len(hostname) > MAX_HOSTNAME:
        raise ValueError("preview hostname is too long")
    return hostname


def pull_request_hostname_candidate(
    *, domain: str, project_slug: str, pr_number: int, git_sha: str
) -> str:
    """Build the R-11 hostname for a PR's *first* push.

    The returned value may already exist for the PR; the allocator decides.
    """

    suffix = validate_preview_domain(domain)
    number = validate_pr_number(pr_number)
    return _build_hostname(
        prefix=f"pr-{number}-",
        slug=project_slug,
        sha=git_sha,
        domain=suffix,
    )


def branch_hostname_candidate(
    *, domain: str, project_slug: str, git_ref: str, git_sha: str
) -> str:
    """Hostname for a non-PR preview deployment (R-10's catch-all).

    The spec does not define this format. Keeping it in one function makes the
    proposal easy to apply once the coordinator chooses a convention.
    """

    suffix = validate_preview_domain(domain)
    try:
        validate_git_ref(git_ref)
    except InvalidGitRef as exc:
        raise ValueError("branch preview needs a valid git ref") from exc
    if not git_ref.startswith("refs/heads/"):
        raise ValueError("branch previews are only issued for heads")
    branch = git_ref.removeprefix("refs/heads/")
    return _build_hostname(
        prefix="br-",
        slug=f"{project_slug}-{branch}",
        sha=git_sha,
        domain=suffix,
    )


class PreviewHostnameAllocator:
    """Issue one hostname per PR or branch, forever (R-11).

    The store is responsible for atomic allocate-or-return. The key is explicit
    and opaque: `pr:<number>` or `branch:<ref>`. It is never derived by parsing
    a hostname (R-12).
    """

    def __init__(self, store: PreviewHostnameStore, domain: str) -> None:
        self._store = store
        self._domain = validate_preview_domain(domain)

    @property
    def domain(self) -> str:
        return self._domain

    async def for_pull_request(
        self, project: Project, pr_number: int, git_sha: str
    ) -> str:
        candidate = pull_request_hostname_candidate(
            domain=self._domain,
            project_slug=project.slug,
            pr_number=pr_number,
            git_sha=git_sha,
        )
        return await self._store.allocate_preview_hostname(
            project.id, f"pr:{pr_number}", candidate
        )

    async def for_branch(self, project: Project, git_ref: str, git_sha: str) -> str:
        candidate = branch_hostname_candidate(
            domain=self._domain,
            project_slug=project.slug,
            git_ref=git_ref,
            git_sha=git_sha,
        )
        return await self._store.allocate_preview_hostname(
            project.id, f"branch:{git_ref}", candidate
        )
