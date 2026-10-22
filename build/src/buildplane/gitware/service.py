"""The SPEC-08 webhook receiver and deployment-intent service.

Flow for every delivery:

    raw bytes (bounded) -> derived-key selection -> signature verify over raw
    -> project lookup (same work on every path) -> parse -> route -> 202

The receiver never clones, builds or waits for a queue. Creating deployment
intent is one store call; everything expensive belongs to SPEC-09/11.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from buildplane.gitware.errors import (
    InvalidProviderRepoId,
    WebhookHeaderError,
    WebhookPayloadError,
)
from buildplane.gitware.events import (
    PREVIEW_PR_ACTIONS,
    PingEvent,
    PullRequestEvent,
    PushEvent,
    RepositoryEvent,
    UnsupportedEvent,
    decode_payload,
    event_action,
    parse_event,
    repository_id_from_payload,
)
from buildplane.gitware.models import (
    Environment,
    Project,
    ProjectStatus,
    WebhookEventRecord,
    WebhookOutcome,
)
from buildplane.gitware.previews import PreviewHostnameAllocator
from buildplane.gitware.ratelimit import (
    InMemoryRateLimiter,
    WebhookRateLimiter,
)
from buildplane.queue.errors import QueueBackpressure
from buildplane.gitware.routing import (
    build_pull_request_intent,
    build_push_intent,
    pull_request_ref,
    route_push,
)
from buildplane.gitware.signature import (
    SIGNATURE_HEADER,
    derive_unresolved_secret,
    derive_webhook_secret,
    verify_signature,
)
from buildplane.gitware.store import GitStore
from buildplane.gitware.validation import (
    validate_delivery_id,
    validate_master_secret,
    validate_preview_domain,
    validate_provider_repo_id,
)

MAX_WEBHOOK_BYTES = 25 * 1024 * 1024  # GitHub's maximum payload is 25 MB.
WEBHOOK_EVENT_HEADER = "x-github-event"
DELIVERY_HEADER = "x-github-delivery"

_logger = logging.getLogger("buildplane.gitware.webhook")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject(action: str, *, status_code: int = 400) -> WebhookOutcome:
    return WebhookOutcome(status_code=status_code, action=action, error=action)


class WebhookService:
    def __init__(
        self,
        store: GitStore,
        *,
        master_secret: bytes,
        preview_domain: str,
        rate_limiter: WebhookRateLimiter | None = None,
        now: Callable[[], datetime] = _utc_now,
        max_body_bytes: int = MAX_WEBHOOK_BYTES,
        logger: logging.Logger | None = None,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self._store = store
        self._master = validate_master_secret(master_secret)
        self._preview_domain = validate_preview_domain(preview_domain)
        self._previews = PreviewHostnameAllocator(store, self._preview_domain)
        self._rate_limiter = rate_limiter or InMemoryRateLimiter()
        self._now = now
        self._max_body_bytes = max_body_bytes
        self._logger = logger or _logger

    @property
    def max_body_bytes(self) -> int:
        return self._max_body_bytes

    @property
    def preview_domain(self) -> str:
        return self._preview_domain

    @property
    def rate_limiter(self) -> WebhookRateLimiter:
        return self._rate_limiter

    async def handle(
        self,
        raw: bytes,
        headers: Mapping[str, str],
        *,
        repo_id_hint: int | None = None,
    ) -> WebhookOutcome:
        """Process one webhook delivery. Domain outcomes are returned, not raised.

        `repo_id_hint` is an optional extension for registrations that put the
        numeric repository ID in the webhook URL. It lets a deployment verify
        the signature before parsing anything at all; without it the receiver
        reads only `repository.id` to select the derived key (R-5), then
        verifies the raw bytes before using any other field.
        """

        if not isinstance(raw, (bytes, bytearray)):
            raise TypeError("raw webhook body must be bytes")
        body = bytes(raw)
        if len(body) > self._max_body_bytes:
            return _reject("payload_too_large", status_code=413)

        header_map = {str(key).lower(): str(value) for key, value in headers.items()}
        signature = header_map.get(SIGNATURE_HEADER)
        event_type = header_map.get(WEBHOOK_EVENT_HEADER)
        delivery_header = header_map.get(DELIVERY_HEADER)

        hint: int | None = None
        if repo_id_hint is not None:
            try:
                hint = validate_provider_repo_id(repo_id_hint)
            except InvalidProviderRepoId:
                return _reject("invalid_payload")

        # This is the R-3/R-5 reconciliation. The body is decoded once; only
        # `repository.id` is read to choose the key. Nothing else from the
        # document is used until the signature check below succeeds.
        payload = decode_payload(body)
        repo_id = hint if hint is not None else repository_id_from_payload(payload)
        if repo_id is None:
            secret = derive_unresolved_secret(self._master)
        else:
            secret = derive_webhook_secret(self._master, repo_id)
        signature_ok = verify_signature(body, signature, secret)

        # R-6/AC-2: perform the lookup on both the valid and invalid paths.
        # An attacker cannot learn whether a repo exists from a cheap
        # early-return or from a missing database call.
        project = (
            await self._store.find_project("github", repo_id)
            if repo_id is not None
            else None
        )

        if not signature_ok:
            self._logger.debug(
                "rejected GitHub webhook signature delivery=%s repo_id=%s",
                delivery_header,
                repo_id,
            )
            return _reject("invalid_signature")

        if payload is None or not event_type or repo_id is None:
            return _reject("invalid_payload")

        payload_repo_id = repository_id_from_payload(payload)
        if payload_repo_id != repo_id:
            # A signed payload must be for the repository its URL selected.
            return _reject("invalid_payload")

        if project is None:
            # AC-2: stop at the same point as the invalid-signature path. An
            # unknown repo needs no delivery claim, no event parse and no audit
            # row, so there is no extra work to distinguish by timing.
            self._logger.debug(
                "verified webhook for unknown repository delivery=%s repo_id=%s",
                delivery_header,
                repo_id,
            )
            return WebhookOutcome(202, "unknown_repo")

        try:
            delivery_id = validate_delivery_id(delivery_header)
        except WebhookHeaderError:
            return _reject("invalid_payload")

        if not await self._store.claim_delivery(delivery_id):
            return WebhookOutcome(202, "duplicate")

        try:
            return await self._dispatch(
                project=project,
                payload=payload,
                event_type=event_type,
                delivery_id=delivery_id,
                now=self._now(),
            )
        except QueueBackpressure as exc:
            # R-6: a deliberate backpressure decision is a domain outcome, not
            # an infrastructure failure. Release the delivery so a later retry
            # can enqueue once capacity returns; return quickly either way.
            try:
                await self._store.release_delivery(delivery_id)
            except Exception:  # pragma: no cover - best effort
                self._logger.warning(
                    "could not release backpressured delivery=%s",
                    delivery_id,
                    exc_info=True,
                )
            return WebhookOutcome(
                status_code=exc.status_code,
                action="queue_full",
                error=exc.code,
                detail={"retry_after_seconds": str(exc.retry_after_seconds)},
            )
        except Exception:
            # Let GitHub retry, but do not turn the delivery into a permanent
            # duplicate. Domain outcomes above never reach this block.
            try:
                await self._store.release_delivery(delivery_id)
            except Exception:  # pragma: no cover - best effort
                self._logger.warning(
                    "could not release webhook delivery=%s after failure",
                    delivery_id,
                    exc_info=True,
                )
            raise

    async def _dispatch(
        self,
        *,
        project: Project,
        payload: dict[str, object],
        event_type: str,
        delivery_id: str,
        now: datetime,
    ) -> WebhookOutcome:
        try:
            event = parse_event(event_type, payload)
        except WebhookPayloadError as exc:
            # The message names a field, never its value.
            self._logger.debug("invalid signed webhook payload: %s", exc)
            return _reject("invalid_payload")

        action = event_action(payload)
        git_ref, git_sha = _event_facts(event)

        await self._record_rate_limit(project, now)

        if project.status is not ProjectStatus.ACTIVE:
            outcome_name = f"project_{project.status.value}"
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                outcome=outcome_name,
                at=now,
                git_ref=git_ref,
                git_sha=git_sha,
            )
            return WebhookOutcome(202, outcome_name)

        if not project.verified:
            # R-8 blocks the deployment, but GitHub must still receive 202 so
            # it does not retry a webhook that will remain unverified.
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                outcome="blocked_unverified",
                at=now,
                git_ref=git_ref,
                git_sha=git_sha,
            )
            return WebhookOutcome(202, "blocked_unverified")

        if isinstance(event, PushEvent):
            return await self._handle_push(
                project, event, delivery_id=delivery_id, now=now
            )
        if isinstance(event, PullRequestEvent):
            return await self._handle_pull_request(
                project, event, delivery_id=delivery_id, now=now
            )
        if isinstance(event, RepositoryEvent):
            return await self._handle_repository(
                project, event, delivery_id=delivery_id, now=now
            )
        if isinstance(event, PingEvent):
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                outcome="pong",
                at=now,
            )
            return WebhookOutcome(202, "pong")
        if isinstance(event, UnsupportedEvent):
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type=event_type,
                action=action,
                outcome="ignored_event",
                at=now,
            )
            return WebhookOutcome(202, "ignored_event")
        return WebhookOutcome(202, "ignored_event")  # pragma: no cover

    async def _handle_push(
        self,
        project: Project,
        event: PushEvent,
        *,
        delivery_id: str,
        now: datetime,
    ) -> WebhookOutcome:
        if event.deleted:
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="push",
                action=None,
                outcome="ignored_branch_deleted",
                at=now,
                git_ref=event.ref,
            )
            return WebhookOutcome(202, "ignored_branch_deleted")

        if not event.is_branch:
            # Tags are not deployment refs in SPEC-08.
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="push",
                action=None,
                outcome="ignored_ref",
                at=now,
                git_ref=event.ref,
                git_sha=event.after,
            )
            return WebhookOutcome(202, "ignored_ref")

        rules = await self._store.list_branch_rules(project.id)
        match = route_push(project, rules, event.ref)
        if match is None:
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="push",
                action=None,
                outcome="ignored_unrouted",
                at=now,
                git_ref=event.ref,
                git_sha=event.after,
            )
            return WebhookOutcome(202, "ignored_unrouted")

        if not match.auto_deploy:
            # AC-5: no deployment, but still 202 (R-7).
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="push",
                action=None,
                outcome="ignored_auto_deploy_off",
                at=now,
                git_ref=event.ref,
                git_sha=event.after,
            )
            return WebhookOutcome(202, "ignored_auto_deploy_off")

        preview_hostname = None
        if match.environment is Environment.PREVIEW:
            preview_hostname = await self._previews.for_branch(
                project, event.ref, event.after
            )

        intent = build_push_intent(
            project,
            match,
            git_sha=event.after,
            git_ref=event.ref,
            delivery_id=delivery_id,
            preview_hostname=preview_hostname,
        )
        result = await self._store.record_intent(intent, received_at=now)
        outcome_name = "deployment_queued" if result.created else "duplicate_sha"
        await self._record_event(
            project,
            delivery_id=delivery_id,
            event_type="push",
            action=None,
            outcome=outcome_name,
            at=now,
            git_ref=event.ref,
            git_sha=event.after,
            deployment_id=result.deployment_id,
        )
        detail = (
            {"queue_position": str(result.queue_position)}
            if result.queue_position is not None
            else {}
        )
        return WebhookOutcome(
            202,
            outcome_name,
            deployment_id=result.deployment_id,
            detail=detail,
        )

    async def _handle_pull_request(
        self,
        project: Project,
        event: PullRequestEvent,
        *,
        delivery_id: str,
        now: datetime,
    ) -> WebhookOutcome:
        if event.action not in PREVIEW_PR_ACTIONS:
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="pull_request",
                action=event.action,
                outcome="ignored_action",
                at=now,
                git_ref=pull_request_ref(event.number),
                git_sha=event.head_sha,
            )
            return WebhookOutcome(202, "ignored_action")

        if event.head_repo_id is None:
            # The fork was deleted; there is no commit left to build.
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="pull_request",
                action=event.action,
                outcome="ignored_head_repo",
                at=now,
                git_ref=pull_request_ref(event.number),
                git_sha=event.head_sha,
            )
            return WebhookOutcome(202, "ignored_head_repo")

        # R-11/AC-4: allocate once per PR, then reuse it for every push. The
        # candidate contains the first push's SHA, but the store's key is the
        # PR, so the second push gets the first hostname back.
        preview_hostname = await self._previews.for_pull_request(
            project, event.number, event.head_sha
        )
        intent = build_pull_request_intent(
            project,
            pr_number=event.number,
            git_sha=event.head_sha,
            preview_hostname=preview_hostname,
            delivery_id=delivery_id,
        )
        result = await self._store.record_intent(intent, received_at=now)
        outcome_name = "deployment_queued" if result.created else "duplicate_sha"
        await self._record_event(
            project,
            delivery_id=delivery_id,
            event_type="pull_request",
            action=event.action,
            outcome=outcome_name,
            at=now,
            git_ref=pull_request_ref(event.number),
            git_sha=event.head_sha,
            deployment_id=result.deployment_id,
        )
        detail = (
            {"queue_position": str(result.queue_position)}
            if result.queue_position is not None
            else {}
        )
        return WebhookOutcome(
            202,
            outcome_name,
            deployment_id=result.deployment_id,
            detail=detail,
        )

    async def _handle_repository(
        self,
        project: Project,
        event: RepositoryEvent,
        *,
        delivery_id: str,
        now: datetime,
    ) -> WebhookOutcome:
        if event.action != "deleted":
            await self._record_event(
                project,
                delivery_id=delivery_id,
                event_type="repository",
                action=event.action,
                outcome="ignored_action",
                at=now,
            )
            return WebhookOutcome(202, "ignored_action")

        await self._orphan(project, reason="repository_deleted", at=now)
        await self._record_event(
            project,
            delivery_id=delivery_id,
            event_type="repository",
            action=event.action,
            outcome="orphaned",
            at=now,
        )
        return WebhookOutcome(202, "orphaned")

    async def handle_repository_unavailable(
        self, project_id: str, *, reason: str = "github_410"
    ) -> int:
        """R-18: GitHub answered 410 for the repo; stop builds, keep the data.

        SPEC-08 registers push and pull_request events, not `repository`, so
        the real-world trigger is the API's 410 during a fetch/verification.
        The `repository` webhook action is handled too for deployments that
        subscribe to it.
        """

        project = await self._store.get_project(project_id)
        if project is None:
            return 0
        return await self._orphan(project, reason=reason, at=self._now())

    async def handle_token_revoked(
        self, project_id: str, *, reason: str = "token_revoked"
    ) -> int:
        """The token-revoked failure row: no builds until reconnect."""

        project = await self._store.get_project(project_id)
        if project is None:
            return 0
        now = self._now()
        await self._store.set_project_status(
            project.id, ProjectStatus.CREDENTIAL_INVALID
        )
        return await self._store.cancel_queued(
            project.id, reason=reason, at=now
        )

    async def _orphan(
        self, project: Project, *, reason: str, at: datetime
    ) -> int:
        await self._store.set_project_status(
            project.id, ProjectStatus.ORPHANED
        )
        return await self._store.cancel_queued(
            project.id, reason=reason, at=at
        )

    async def _record_rate_limit(
        self, project: Project, now: datetime
    ) -> None:
        try:
            await self._rate_limiter.record(project.id, now=now)
        except Exception:
            # R-17 is observability. A limiter outage must not drop a deploy.
            self._logger.warning(
                "webhook rate limiter failed project_id=%s", project.id,
                exc_info=True,
            )

    async def _record_event(
        self,
        project: Project,
        *,
        delivery_id: str,
        event_type: str,
        action: str | None,
        outcome: str,
        at: datetime,
        git_ref: str | None = None,
        git_sha: str | None = None,
        deployment_id: str | None = None,
    ) -> None:
        record = WebhookEventRecord(
            delivery_id=delivery_id,
            project_id=project.id,
            event=event_type,
            action=action,
            outcome=outcome,
            received_at=at,
            git_ref=git_ref,
            git_sha=git_sha,
            provider_repo_id=project.provider_repo_id,
            deployment_id=deployment_id,
        )
        try:
            await self._store.record_event(record)
        except Exception:
            # Debuggability is valuable, but it is not worth failing a webhook
            # or leaking the payload into a log to get it.
            self._logger.warning(
                "could not record webhook metadata delivery=%s", delivery_id,
                exc_info=True,
            )


def _event_facts(event: object) -> tuple[str | None, str | None]:
    if isinstance(event, PushEvent):
        return event.ref, event.after
    if isinstance(event, PullRequestEvent):
        return pull_request_ref(event.number), event.head_sha
    return None, None
