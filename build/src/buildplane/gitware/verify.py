"""Repository ownership verification (SPEC-08 R-8, R-9).

Two methods are supported, and at least one must complete before a project can
deploy:

* a DNS TXT record;
* a token file on a branch of the connected repository.

The DNS and GitHub calls are ports (`TxtResolver`, `RepoReader`). This package
never holds an OAuth token and never performs network I/O itself; the control
plane supplies an adapter scoped to the connected repository.

The spec's TXT name is `_<token>.connect.yourpaas.com`, which is a name the
platform controls rather than the customer. It is implemented literally here
and logged as a spec-change proposal; isolating it in `expected_txt_name` means
a corrected suffix is a one-line change.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from buildplane.gitware.errors import (
    DeploymentBlocked,
    InvalidVerificationToken,
    VerificationUnavailable,
)
from buildplane.gitware.models import (
    Project,
    ProjectStatus,
    VerificationMethod,
)
from buildplane.gitware.validation import validate_verification_token

TXT_SUFFIX = "connect.yourpaas.com"
VERIFY_BRANCH_PREFIX = "autopilot-verify-"
VERIFY_FILE = ".autopilot/verify"
MAX_VERIFY_BRANCHES = 100
DEFAULT_REVERIFY_INTERVAL = timedelta(hours=24)


class TxtResolver(Protocol):
    async def records(self, name: str) -> Sequence[str]:
        """Return the TXT record strings for `name`."""


class RepoReader(Protocol):
    async def list_branches(self) -> Sequence[str]:
        """Return branch names, without the `refs/heads/` prefix."""

    async def read_file(self, branch: str, path: str) -> str | None:
        """Return a file's text on `branch`, or None when it does not exist."""


class VerificationStore(Protocol):
    async def mark_verified(
        self, project_id: str, method: VerificationMethod, *, at: datetime
    ) -> None: ...

    async def mark_unverified(
        self, project_id: str, *, reason: str, at: datetime
    ) -> None: ...

    async def cancel_queued(self, project_id: str, *, reason: str, at: datetime) -> int: ...


@dataclass(frozen=True, slots=True)
class VerificationResult:
    verified: bool
    method: VerificationMethod
    detail: str


def new_verification_token() -> str:
    """Generate a DNS-safe token (32 lowercase hex characters)."""

    return secrets.token_hex(16)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _token_matches(candidate: object, token: str) -> bool:
    """Constant-time comparison of a DNS TXT value or file body with a token."""

    if not isinstance(candidate, str):
        return False
    stripped = candidate.strip().strip('"').strip()
    try:
        return hmac.compare_digest(stripped, token)
    except TypeError:
        # Non-ASCII input cannot equal an ASCII token.
        return False


class OwnershipVerifier:
    def __init__(
        self,
        store: VerificationStore,
        *,
        now: Callable[[], datetime] = _utc_now,
        reverify_interval: timedelta = DEFAULT_REVERIFY_INTERVAL,
    ) -> None:
        self._store = store
        self._now = now
        self._reverify_interval = reverify_interval

    @staticmethod
    def expected_txt_name(project: Project) -> str:
        """Return the TXT name the customer is asked to create.

        The token is validated before it is interpolated into a DNS label, so a
        corrupt database value cannot produce a wildcard or traversal value.
        """

        try:
            token = validate_verification_token(project.verification_token)
        except InvalidVerificationToken as exc:
            raise VerificationUnavailable("project has no usable verification token") from exc
        return f"_{token}.{TXT_SUFFIX}"

    async def verify_txt(self, project: Project, resolver: TxtResolver) -> VerificationResult:
        name = self.expected_txt_name(project)
        try:
            records = await resolver.records(name)
        except Exception as exc:  # DNS outage is not a negative proof.
            raise VerificationUnavailable("TXT lookup failed") from exc

        token = project.verification_token
        if any(_token_matches(record, token) for record in records):
            await self._store.mark_verified(
                project.id, VerificationMethod.TXT, at=self._now()
            )
            return VerificationResult(True, VerificationMethod.TXT, "txt_record_found")
        return VerificationResult(False, VerificationMethod.TXT, "txt_record_missing")

    async def verify_deploy_branch(
        self, project: Project, reader: RepoReader
    ) -> VerificationResult:
        try:
            token = validate_verification_token(project.verification_token)
        except InvalidVerificationToken as exc:
            raise VerificationUnavailable("project has no usable verification token") from exc

        try:
            branches = list(await reader.list_branches())
        except Exception as exc:
            raise VerificationUnavailable("branch listing failed") from exc

        candidates = [b for b in branches if b.startswith(VERIFY_BRANCH_PREFIX)]
        candidates = candidates[:MAX_VERIFY_BRANCHES]
        read_failures = 0
        for branch in candidates:
            try:
                content = await reader.read_file(branch, VERIFY_FILE)
            except Exception:
                read_failures += 1
                continue
            if content is not None and _token_matches(content, token):
                await self._store.mark_verified(
                    project.id, VerificationMethod.DEPLOY_BRANCH, at=self._now()
                )
                return VerificationResult(
                    True,
                    VerificationMethod.DEPLOY_BRANCH,
                    "verification_file_found",
                )

        if read_failures:
            # One unreadable candidate means we cannot conclude the proof is
            # gone. Returning False here would un-verify a healthy project (and
            # cancel queued builds) because of a transient GitHub error.
            raise VerificationUnavailable("could not read all verification branches")
        return VerificationResult(
            False, VerificationMethod.DEPLOY_BRANCH, "verification_file_missing"
        )

    def verification_due(self, project: Project) -> bool:
        """True when R-9's scheduled re-check is due."""

        if not project.verified:
            return False
        checked = project.verification_checked_at
        if checked is None:
            return True
        return _as_utc(self._now()) - _as_utc(checked) >= self._reverify_interval

    async def reverify(
        self,
        project: Project,
        method: VerificationMethod,
        *,
        resolver: TxtResolver | None = None,
        reader: RepoReader | None = None,
    ) -> VerificationResult:
        """Re-run the recorded method and fail closed if it no longer holds.

        A negative result un-verifies the project and cancels queued builds.
        An infrastructure failure raises `VerificationUnavailable` and leaves
        the existing verification intact: an outage is not proof of takeover.
        """

        if method is VerificationMethod.TXT:
            if resolver is None:
                raise ValueError("TXT reverification needs a resolver")
            result = await self.verify_txt(project, resolver)
        elif method is VerificationMethod.DEPLOY_BRANCH:
            if reader is None:
                raise ValueError("branch reverification needs a reader")
            result = await self.verify_deploy_branch(project, reader)
        else:  # pragma: no cover - guarded by the enum
            raise ValueError(f"unknown verification method: {method}")

        if not result.verified:
            now = self._now()
            await self._store.mark_unverified(
                project.id, reason=result.detail, at=now
            )
            await self._store.cancel_queued(
                project.id, reason="verification_lost", at=now
            )
        return result

    async def ensure_deployable(self, project: Project) -> None:
        """The R-8 gate shared by every deployment path.

        SPEC-04's `POST /deployments` must surface `domain_unverified` as 403.
        Webhook deliveries do not raise; they record the event and answer 202
        so GitHub does not retry (see service.py).
        """

        if project.status is ProjectStatus.SUSPENDED:
            raise DeploymentBlocked("tenant_suspended")
        if project.status is ProjectStatus.ORPHANED:
            raise DeploymentBlocked("project_orphaned")
        if project.status is ProjectStatus.CREDENTIAL_INVALID:
            raise DeploymentBlocked("credential_invalid")
        if project.status is not ProjectStatus.ACTIVE:
            raise DeploymentBlocked("project_unavailable")
        if not project.verified:
            raise DeploymentBlocked("domain_unverified")
