"""Result reporting for SPEC-09 build jobs.

A successful build records the resolved manifest and the reproducible inputs but
does **not** mark the deployment ready: SPEC-12 must push the image and SPEC-13's
verification gate must pass first. A failed build is reported as a terminal
failure; a transient failure returns the job to pending with backoff and the
deployment to queued, exactly as the job-lifecycle table requires.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import AsyncIterator, Protocol

import asyncpg
from platform_shared.manifest import canonical_manifest_json

from buildplane.pipeline.models import BuildOutcome, ClaimedJob
from buildplane.queue.retry import backoff_seconds, decide_retry, stable_jitter

_RATE_LIMIT_ERROR_CODES = frozenset({"registry_rate_limited", "registry_rate_limit"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class BuildReporter(Protocol):
    async def report_success(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None: ...

    async def report_failure(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None: ...


class InMemoryBuildReporter:
    def __init__(self) -> None:
        self.successes: list[tuple[ClaimedJob, BuildOutcome]] = []
        self.failures: list[tuple[ClaimedJob, BuildOutcome]] = []
        self.cancelled: list[tuple[ClaimedJob, BuildOutcome]] = []

    async def report_success(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        self.successes.append((claim, outcome))

    async def report_failure(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        self.failures.append((claim, outcome))

    async def report_cancelled(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        self.cancelled.append((claim, outcome))


class PostgresBuildReporter:
    def __init__(self, database, *, clock=_utcnow) -> None:
        self._db = database
        self._clock = clock

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        if hasattr(self._db, "acquire"):
            async with self._db.acquire() as connection:
                yield connection
        else:
            yield self._db

    async def report_success(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        if outcome.manifest is None or outcome.manifest_hash is None:
            raise ValueError("successful build must record manifest and manifest_hash")
        if outcome.build_image is None:
            raise ValueError("successful build must record build_image")
        manifest_json, expected_hash = canonical_manifest_json(outcome.manifest)
        if expected_hash != outcome.manifest_hash:
            raise ValueError("manifest_hash does not match the recorded manifest")
        now = self._clock()
        async with self._connection() as conn:
            async with conn.transaction():
                deployment_status = await conn.fetchval(
                    "SELECT status FROM deployments WHERE id = $1 FOR UPDATE",
                    _uuid(claim.deployment_id),
                )
                if deployment_status in {"cancelled", "superseded"}:
                    # R-17: a late success must not resurrect cancelled work.
                    await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'cancelled', last_error = 'cancelled',
                            heartbeat_at = NULL, lease_expires_at = NULL,
                            updated_at = $1
                        WHERE id = $2 AND status = 'running'
                        """,
                        now,
                        _uuid(claim.job_id),
                    )
                    return
                result = await conn.execute(
                    """
                    UPDATE build_jobs
                    SET status = 'succeeded', last_error = NULL,
                        heartbeat_at = NULL, lease_expires_at = NULL,
                        updated_at = $1
                    WHERE id = $2 AND status = 'running'
                    """,
                    now,
                    _uuid(claim.job_id),
                )
                if result != "UPDATE 1":
                    # Another terminal transition won the race.
                    return
                # A build-only success stays at stage=built and is not ready:
                # SPEC-12 publication is the step that supplies the digest.
                # When a publisher ran, the row becomes ready atomically with
                # the image record, and SPEC-13's runtime gate may deploy it.
                await conn.execute(
                    """
                    UPDATE deployments
                    SET manifest = $1::jsonb,
                        manifest_hash = $2,
                        build_image = $3,
                        build_env_keys = $4,
                        stage = CASE
                            WHEN $6::text IS NOT NULL AND $7::text IS NOT NULL
                            THEN 'pushed' ELSE 'built'
                        END,
                        status = CASE
                            WHEN $6::text IS NOT NULL AND $7::text IS NOT NULL
                            THEN 'ready'::deployment_status ELSE status
                        END,
                        revision = COALESCE($6, revision),
                        image_digest = COALESCE($7, image_digest),
                        exit_code = 0,
                        error_message = NULL,
                        log_uri = $5,
                        node_version = $8
                    WHERE id = $9 AND status = 'building'
                    """,
                    manifest_json,
                    outcome.manifest_hash,
                    outcome.build_image,
                    list(claim.build_env_keys),
                    outcome.log_uri,
                    outcome.revision,
                    outcome.image_digest,
                    outcome.node_version,
                    _uuid(claim.deployment_id),
                )
                if (
                    outcome.revision is not None
                    and outcome.image_digest is not None
                    and outcome.image_registry is not None
                    and outcome.image_repository is not None
                ):
                    await conn.execute(
                        """
                        INSERT INTO images
                            (org_id, project_id, deployment_id, tag, digest,
                             registry, size_bytes, build_image_digest, manifest_hash)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        ON CONFLICT (digest) DO NOTHING
                        """,
                        _uuid(claim.org_id),
                        _uuid(claim.project_id),
                        _uuid(claim.deployment_id),
                        outcome.revision,
                        outcome.image_digest,
                        outcome.image_registry,
                        int(outcome.image_size_bytes or 0),
                        _builder_digest(outcome.build_image),
                        outcome.manifest_hash,
                    )

    async def report_failure(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        now = self._clock()
        error = (outcome.error or "build failed")[:4000]
        error_code = outcome.error_code or "build_failed"
        recorded_error = f"{error_code}: {error}"
        async with self._connection() as conn:
            async with conn.transaction():
                deployment_status = await conn.fetchval(
                    "SELECT status FROM deployments WHERE id = $1 FOR UPDATE",
                    _uuid(claim.deployment_id),
                )
                if deployment_status in {"cancelled", "superseded"}:
                    # R-17: cancellation beats retry (and beats a late
                    # dead-letter transition). Leave the deployment alone; its
                    # row is already terminal.
                    await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'cancelled', last_error = 'cancelled',
                            worker_id = NULL, heartbeat_at = NULL,
                            lease_expires_at = NULL, updated_at = $1
                        WHERE id = $2 AND status IN ('pending', 'running')
                        """,
                        now,
                        _uuid(claim.job_id),
                    )
                    return

                decision = decide_retry(
                    attempts=claim.attempts,
                    max_attempts=claim.max_attempts,
                    retryable=outcome.retryable,
                )
                if decision.dead_letter:
                    # R-5: retain the row for inspection instead of silently
                    # dropping an exhausted job.
                    result = await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'dead', attempts = $1,
                            worker_id = NULL, heartbeat_at = NULL,
                            lease_expires_at = NULL, last_error = $2,
                            updated_at = $3
                        WHERE id = $4 AND status = 'running'
                        """,
                        decision.next_attempt,
                        recorded_error,
                        now,
                        _uuid(claim.job_id),
                    )
                    if result != "UPDATE 1":
                        # A lease sweep or cancellation already took the job;
                        # a late failure must not rewrite the deployment.
                        return
                    await conn.execute(
                        """
                        UPDATE deployments
                        SET status = 'failed', stage = $1, exit_code = $2,
                            error_message = $3, log_uri = $4, finished_at = $5
                        WHERE id = $6 AND status IN ('building', 'queued')
                        """,
                        str(outcome.stage),
                        outcome.exit_code,
                        error,
                        outcome.log_uri,
                        now,
                        _uuid(claim.deployment_id),
                    )
                elif decision.retry:
                    delay = decision.delay_seconds or 0
                    if error_code in _RATE_LIMIT_ERROR_CODES:
                        delay += stable_jitter(claim.job_id)
                    available = _add_seconds(now, delay)
                    result = await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'pending', attempts = $1,
                            worker_id = NULL, heartbeat_at = NULL,
                            lease_expires_at = NULL, available_at = $2,
                            last_error = $3, updated_at = $4
                        WHERE id = $5 AND status = 'running'
                        """,
                        decision.next_attempt,
                        available,
                        recorded_error,
                        now,
                        _uuid(claim.job_id),
                    )
                    if result != "UPDATE 1":
                        return
                    await conn.execute(
                        """
                        UPDATE deployments
                        SET status = 'queued', stage = $1, exit_code = $2,
                            error_message = $3, log_uri = $4,
                            started_at = NULL, finished_at = NULL
                        WHERE id = $5 AND status = 'building'
                        """,
                        str(outcome.stage),
                        outcome.exit_code,
                        error,
                        outcome.log_uri,
                        _uuid(claim.deployment_id),
                    )
                else:
                    result = await conn.execute(
                        """
                        UPDATE build_jobs
                        SET status = 'failed', worker_id = NULL,
                            last_error = $1, updated_at = $2
                        WHERE id = $3 AND status = 'running'
                        """,
                        recorded_error,
                        now,
                        _uuid(claim.job_id),
                    )
                    if result != "UPDATE 1":
                        return
                    await conn.execute(
                        """
                        UPDATE deployments
                        SET status = 'failed', stage = $1, exit_code = $2,
                            error_message = $3, log_uri = $4, finished_at = $5
                        WHERE id = $6 AND status IN ('building', 'queued')
                        """,
                        str(outcome.stage),
                        outcome.exit_code,
                        error,
                        outcome.log_uri,
                        now,
                        _uuid(claim.deployment_id),
                    )

    async def report_cancelled(
        self, claim: ClaimedJob, outcome: BuildOutcome
    ) -> None:
        now = self._clock()
        async with self._connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE build_jobs
                    SET status = 'cancelled', last_error = 'cancelled', updated_at = $1
                    WHERE id = $2 AND status IN ('pending', 'running')
                    """,
                    now,
                    _uuid(claim.job_id),
                )
                await conn.execute(
                    """
                    UPDATE deployments
                    SET status = 'cancelled', stage = $1, exit_code = $2,
                        error_message = $3, log_uri = $4, finished_at = $5
                    WHERE id = $6 AND status IN ('queued', 'building')
                    """,
                    str(outcome.stage),
                    outcome.exit_code,
                    (outcome.error or "cancelled")[:4000],
                    outcome.log_uri,
                    now,
                    _uuid(claim.deployment_id),
                )


def _add_seconds(value: datetime, seconds: int) -> datetime:
    from datetime import timedelta

    return value + timedelta(seconds=seconds)


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"not a UUID: {value!r}") from exc


def _builder_digest(image: str | None) -> str:
    if image and "@" in image:
        candidate = image.rsplit("@", 1)[1]
        if len(candidate) == 71 and candidate.startswith("sha256:"):
            return candidate
    return "sha256:" + "0" * 64
