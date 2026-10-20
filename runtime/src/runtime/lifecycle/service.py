"""SPEC-13 deployment lifecycle service.

The service takes an ``asyncpg`` connection owned by the caller, exactly like the
SPEC-18 secrets service: the caller opens the tenant transaction
(``set_config('app.org_id', ..., true)``), so RLS context, audit ordering, the
deployment insert, the build-job enqueue and the route cutover all commit or
roll back together. The service never commits.

What lives here and what does not:

* here - the seven-state machine, idempotent trigger, superseding, promotion
  and rollback transactions, and the drain bound;
* SPEC-11 - claiming/leasing the build job (we only insert it);
* SPEC-14 - rendering, apply, ``wait_ready`` and the actual pod drain. Promotion
  is called only after that pre-warm succeeds (SPEC-14 R-17);
* SPEC-15/16 - allocating route hostnames and converging ingress. A cutover
  requires route rows to exist; it never invents a hostname.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Mapping, Sequence

import asyncpg

from runtime.lifecycle.errors import (
    AuditUnavailable,
    DeploymentNotFound,
    DeploymentNotReady,
    EnqueueFailed,
    InvalidDeployment,
    NoRoute,
    ProjectNotFound,
    RollbackNotAvailable,
)
from runtime.lifecycle.models import (
    Deployment,
    PromotionResult,
    TriggerResult,
    deployment_from_row,
)
from runtime.lifecycle.queue import BuildQueue, PostgresBuildQueue
from runtime.lifecycle.state import (
    DrainPolicy,
    assert_actor_type,
    assert_ready_invariants,
    assert_transition,
    canonical_manifest_json,
    idempotency_key,
    promotion_lock_key,
    validate_actor_id,
    validate_env_keys,
    validate_environment,
    validate_git_ref,
    validate_git_sha,
    validate_hostnames,
    validate_image_digest,
    validate_image_reference,
    validate_manifest_hash,
    validate_revision,
)
from runtime.secrets.audit import AuditRecord, AuditSink

_COLUMNS = (
    "id, org_id, project_id, git_sha, git_ref, environment, status, stage, "
    "manifest, manifest_hash, build_image, build_env_keys, revision, "
    "image_digest, exit_code, error_message, created_at, started_at, "
    "finished_at, superseded_by"
)

_TRIGGER_SOURCES = frozenset({"webhook", "manual", "api", "agent"})
_ROLLBACK_STATUSES = frozenset({"ready", "live", "superseded"})


class DeploymentLifecycle:
    """The SPEC-13 state machine and its transactional operations."""

    def __init__(
        self,
        *,
        audit: AuditSink,
        queue: BuildQueue | None = None,
        drain_policy: DrainPolicy | None = None,
    ) -> None:
        self._audit = audit
        self._queue = queue or PostgresBuildQueue()
        self._drain = drain_policy or DrainPolicy()

    @property
    def drain_policy(self) -> DrainPolicy:
        return self._drain

    # ---------------------------------------------------------------- trigger

    async def trigger(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        git_sha: str,
        git_ref: str,
        environment: str,
        manifest: Mapping[str, Any] | Any,
        build_image: str,
        manifest_hash: str | None = None,
        build_env_keys: Sequence[str] = (),
        trigger_source: str = "webhook",
        triggered_by: str | uuid.UUID | None = None,
        actor_id: str,
        actor_type: str = "system",
        priority: int = 0,
    ) -> TriggerResult:
        """Insert a ``queued`` deployment and its build job, idempotently.

        R-10 deduplicates on ``(project_id, git_sha, git_ref)`` while the first
        deployment is still in flight. Once it has finished (``ready``, ``live``
        or terminal), a repeat is a legitimate redeploy and creates a new row -
        the fourth row of R-10's table.

        The caller must run this inside a transaction. If the enqueue fails, the
        exception propagates and the deployment insert rolls back (§8).
        """
        org = _as_uuid(org_id, "org_id")
        project = _as_uuid(project_id, "project_id")
        sha = validate_git_sha(git_sha)
        ref = validate_git_ref(git_ref)
        environment = validate_environment(environment)
        build_image = validate_image_reference(build_image)
        env_keys = validate_env_keys(build_env_keys)
        actor_id = validate_actor_id(actor_id)
        assert_actor_type(actor_type)
        if trigger_source not in _TRIGGER_SOURCES:
            raise InvalidDeployment(
                f"trigger_source must be one of {sorted(_TRIGGER_SOURCES)}"
            )
        actor_user = _as_uuid(triggered_by, "triggered_by") if triggered_by else None
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise InvalidDeployment("priority must be an integer")

        manifest_json, computed_hash = canonical_manifest_json(manifest)
        manifest_hash = (
            validate_manifest_hash(manifest_hash)
            if manifest_hash is not None
            else computed_hash
        )

        await self._require_project(conn, org, project)
        await self._lock(conn, idempotency_key(project, sha, ref))

        # Literal statuses (not a parameter) let PostgreSQL prove the partial
        # index predicate. Keep them in sync with state.ACTIVE_STATUSES.
        existing = await conn.fetchrow(
            f"SELECT {_COLUMNS} FROM deployments "
            "WHERE org_id = $1 AND project_id = $2 AND git_sha = $3 "
            "  AND git_ref = $4 AND status IN ('queued', 'building') "
            "ORDER BY created_at DESC LIMIT 1",
            org,
            project,
            sha,
            ref,
        )
        if existing is not None:
            return TriggerResult(deployment_from_row(existing), created=False)

        deployment_id = uuid.uuid4()
        await self._audit_or_fail(
            AuditRecord(
                action="deployment.trigger",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="deployment",
                target_id=str(deployment_id),
                metadata={
                    "git_ref": ref,
                    "environment": environment,
                    "manifest_hash": manifest_hash,
                    "trigger_source": trigger_source,
                },
            )
        )

        row = await conn.fetchrow(
            f"INSERT INTO deployments "
            "  (id, org_id, project_id, git_sha, git_ref, environment, "
            "   trigger_source, triggered_by, manifest, manifest_hash, "
            "   build_image, build_env_keys) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12) "
            f"RETURNING {_COLUMNS}",
            deployment_id,
            org,
            project,
            sha,
            ref,
            environment,
            trigger_source,
            actor_user,
            manifest_json,
            manifest_hash,
            build_image,
            list(env_keys),
        )

        try:
            await self._queue.enqueue(
                conn,
                deployment_id=deployment_id,
                org_id=org,
                project_id=project,
                priority=priority,
            )
        except EnqueueFailed:
            raise
        except Exception as exc:  # boundary: any queue failure aborts the trigger
            raise EnqueueFailed(
                "the build job could not be enqueued; trigger rolled back"
            ) from exc

        # R-8: a newer push on the same ref cancels older builds that have not
        # started. Different refs are never touched (R-7).
        cancelled = await conn.fetch(
            "UPDATE deployments "
            "   SET status = 'cancelled', finished_at = now(), "
            "       superseded_by = $5, "
            "       error_message = 'superseded by a newer push' "
            " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
            "   AND status = 'queued' AND created_at < $4 AND id <> $5 "
            "RETURNING id",
            org,
            project,
            ref,
            row["created_at"],
            deployment_id,
        )
        if cancelled:
            await self._cancel_jobs(
                conn, org, [cancelled_row["id"] for cancelled_row in cancelled]
            )

        return TriggerResult(deployment_from_row(row), created=True)

    # ------------------------------------------------------------ build phase

    async def claim(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        stage: str | None = None,
    ) -> Deployment:
        """Move ``queued`` to ``building``. Repeated claims are idempotent."""
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        stage = _validate_stage(stage)

        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])
        if current == "building":
            return deployment_from_row(row)
        assert_transition(current, "building", deployment_id=deployment)

        row = await conn.fetchrow(
            f"UPDATE deployments "
            "   SET status = 'building', "
            "       started_at = COALESCE(started_at, now()), "
            "       stage = COALESCE($3, stage) "
            " WHERE id = $1 AND org_id = $2 "
            f"RETURNING {_COLUMNS}",
            deployment,
            org,
            stage,
        )
        return deployment_from_row(row)

    async def mark_ready(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        revision: str,
        image_digest: str,
    ) -> Deployment:
        """Record the verified revision and move ``building`` to ``ready``.

        If a newer deployment for the same ``(project, git_ref)`` is already
        live, this one is marked ``superseded`` instead, so a slow build can
        never displace the newest commit (R-6). A newer *ready* build does not
        block this one: it will supersede it when it is promoted.
        """
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        revision = validate_revision(revision)
        image_digest = validate_image_digest(image_digest)

        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])
        assert_ready_invariants("ready", revision, image_digest)

        if current in {"ready", "live", "superseded"}:
            # A worker retry after promotion must not fail the build: the
            # revision is already recorded, and re-applying the fact is a no-op.
            if row["revision"] == revision and row["image_digest"] == image_digest:
                return deployment_from_row(row)
            raise InvalidDeployment(
                "a deployment's revision is immutable once recorded"
            )
        assert_transition(current, "ready", deployment_id=deployment)

        newer = await self._newer_live(
            conn,
            org=org,
            project=row["project_id"],
            git_ref=row["git_ref"],
            created_at=row["created_at"],
            exclude_id=deployment,
        )
        if newer is not None:
            row = await conn.fetchrow(
                f"UPDATE deployments "
                "   SET status = 'superseded', finished_at = now(), "
                "       revision = $3, image_digest = $4, superseded_by = $5 "
                " WHERE id = $1 AND org_id = $2 "
                f"RETURNING {_COLUMNS}",
                deployment,
                org,
                revision,
                image_digest,
                newer,
            )
            return deployment_from_row(row)

        row = await conn.fetchrow(
            f"UPDATE deployments "
            "   SET status = 'ready', revision = $3, image_digest = $4, "
            "       error_message = NULL, exit_code = NULL "
            " WHERE id = $1 AND org_id = $2 "
            f"RETURNING {_COLUMNS}",
            deployment,
            org,
            revision,
            image_digest,
        )
        return deployment_from_row(row)

    async def mark_failed(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        stage: str | None = None,
        error_message: str | None = None,
        exit_code: int | None = None,
    ) -> Deployment:
        """Move ``queued``/``building``/``ready`` to ``failed`` (AC-2).

        ``ready`` is included because a failed rollout happens after the build
        succeeded. The route is deliberately untouched: the old revision keeps
        serving (AC-6).
        """
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        stage = _validate_stage(stage)
        error_message = _validate_message(error_message)
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise InvalidDeployment("exit_code must be an integer")

        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])
        if current == "failed":
            return deployment_from_row(row)
        assert_transition(current, "failed", deployment_id=deployment)

        row = await conn.fetchrow(
            f"UPDATE deployments "
            "   SET status = 'failed', finished_at = now(), "
            "       stage = COALESCE($3, stage), "
            "       error_message = COALESCE($4, error_message), "
            "       exit_code = COALESCE($5, exit_code) "
            " WHERE id = $1 AND org_id = $2 "
            f"RETURNING {_COLUMNS}",
            deployment,
            org,
            stage,
            error_message,
            exit_code,
        )
        if current == "queued":
            # A failure before claim must not leave a pending job a worker can
            # later pick up. A running build owns its own job outcome (SPEC-11).
            await self._cancel_jobs(conn, org, [deployment])
        return deployment_from_row(row)

    async def cancel(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        actor_id: str,
        actor_type: str = "user",
        reason: str | None = "cancelled by the user",
    ) -> Deployment:
        """Cancel a deployment that has not gone live (SPEC-04 ``/cancel``)."""
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        actor_id = validate_actor_id(actor_id)
        assert_actor_type(actor_type)
        reason = _validate_message(reason)

        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])
        if current == "cancelled":
            return deployment_from_row(row)
        assert_transition(current, "cancelled", deployment_id=deployment)

        await self._audit_or_fail(
            AuditRecord(
                action="deployment.cancel",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="deployment",
                target_id=str(deployment),
                metadata={"git_ref": str(row["git_ref"]), "from": current},
            )
        )

        row = await conn.fetchrow(
            f"UPDATE deployments "
            "   SET status = 'cancelled', finished_at = now(), "
            "       error_message = COALESCE($3, error_message) "
            " WHERE id = $1 AND org_id = $2 "
            f"RETURNING {_COLUMNS}",
            deployment,
            org,
            reason,
        )
        await self._cancel_jobs(conn, org, [deployment])
        return deployment_from_row(row)

    # --------------------------------------------------------- cutover phase

    async def promote(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        actor_id: str,
        actor_type: str = "system",
        hostnames: Sequence[str] | None = None,
    ) -> PromotionResult:
        """Move a ``ready`` revision to ``live`` by atomically moving routes.

        Promotion never builds or pulls an image (R-4/R-11). The caller must
        have applied the workload and seen it become ready first (SPEC-14 R-17).

        ``hostnames`` selects the route rows to cut over. Omitted, the service
        moves the routes that currently point at a deployment from the same
        environment. The first deployment needs explicit hostnames because no
        route points at it yet; allocating them belongs to SPEC-15.
        """
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        actor_id = validate_actor_id(actor_id)
        assert_actor_type(actor_type)
        targets = validate_hostnames(hostnames) if hostnames is not None else None
        actor = f"{actor_type}:{actor_id}"

        row = await self._load(conn, org, deployment)
        await self._lock(
            conn, promotion_lock_key(row["project_id"], str(row["git_ref"]))
        )
        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])

        if current == "superseded":
            # The slowest build finishing last must not go live (R-6).
            return PromotionResult(deployment_from_row(row), 0, (), False)
        if current not in {"ready", "live"}:
            raise DeploymentNotReady(
                f"deployment {deployment} is {current!r}; promotion needs 'ready'"
            )

        if current == "ready":
            newer = await self._newer_live(
                conn,
                org=org,
                project=row["project_id"],
                git_ref=row["git_ref"],
                created_at=row["created_at"],
                exclude_id=deployment,
            )
            if newer is not None:
                # A newer commit is already serving; this stale build must not
                # replace it. A newer *ready* build is allowed to race: it will
                # supersede this one when it is promoted (R-6).
                row = await conn.fetchrow(
                    f"UPDATE deployments "
                    "   SET status = 'superseded', finished_at = now(), "
                    "       superseded_by = $3 "
                    " WHERE id = $1 AND org_id = $2 "
                    f"RETURNING {_COLUMNS}",
                    deployment,
                    org,
                    newer,
                )
                return PromotionResult(deployment_from_row(row), 0, (), True)

        if current == "live":
            newer_live = await self._newer_live(
                conn,
                org=org,
                project=row["project_id"],
                git_ref=row["git_ref"],
                created_at=row["created_at"],
                exclude_id=deployment,
            )
            if newer_live is not None:
                raise DeploymentNotReady(
                    "a newer revision is live; use rollback to move backwards"
                )
            other_live = await conn.fetchval(
                "SELECT 1 FROM deployments "
                " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
                "   AND id <> $4 AND status = 'live' LIMIT 1",
                org,
                row["project_id"],
                row["git_ref"],
                deployment,
            )
        else:
            other_live = None

        routes = await self._select_routes(
            conn,
            org=org,
            project=row["project_id"],
            environment=str(row["environment"]),
            hostnames=targets,
        )
        route_changes = [
            route
            for route in routes
            if route["deployment_id"] != deployment
            or route["revision"] != row["revision"]
        ]

        if current == "live" and not route_changes and not other_live:
            return PromotionResult(deployment_from_row(row), 0, (), False)

        await self._audit_or_fail(
            AuditRecord(
                action="deployment.promote",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="deployment",
                target_id=str(deployment),
                metadata={
                    "git_ref": str(row["git_ref"]),
                    "revision": str(row["revision"]),
                    "hostnames": [str(route["hostname"]) for route in routes],
                },
            )
        )

        superseded: tuple[uuid.UUID, ...] = ()
        if current == "ready":
            superseded = await self._supersede_older(
                conn,
                org=org,
                project=row["project_id"],
                git_ref=str(row["git_ref"]),
                created_before=row["created_at"],
                exclude_id=deployment,
            )
        elif other_live:
            # Repair a second live row if one exists; promotion from ready
            # already supersedes the previous live, so this is a safety net.
            superseded = await self._supersede_other_live(
                conn,
                org=org,
                project=row["project_id"],
                git_ref=str(row["git_ref"]),
                target_id=deployment,
            )

        updated = await self._cutover_routes(
            conn,
            org=org,
            project=row["project_id"],
            deployment=deployment_from_row(row),
            routes=route_changes,
            actor=actor,
        )

        if current == "ready":
            await conn.execute(
                "UPDATE deployments SET status = 'live' "
                " WHERE id = $1 AND org_id = $2",
                deployment,
                org,
            )

        final = await self._load(conn, org, deployment)
        return PromotionResult(
            deployment_from_row(final),
            updated,
            superseded,
            changed=bool(updated or superseded or current == "ready"),
        )

    async def rollback(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
        actor_id: str,
        actor_type: str = "user",
        hostnames: Sequence[str] | None = None,
    ) -> PromotionResult:
        """Point the routes back at a retained revision in one transaction.

        No rebuild, no image pull (R-11/R-12). The target may be ``superseded``:
        rollback is the one transition that resurrects a revision.
        """
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        actor_id = validate_actor_id(actor_id)
        assert_actor_type(actor_type)
        targets = validate_hostnames(hostnames) if hostnames is not None else None
        actor = f"{actor_type}:{actor_id}"

        row = await self._load(conn, org, deployment)
        await self._lock(
            conn, promotion_lock_key(row["project_id"], str(row["git_ref"]))
        )
        row = await self._load(conn, org, deployment, for_update=True)
        current = str(row["status"])

        if current not in _ROLLBACK_STATUSES or not row["revision"]:
            raise RollbackNotAvailable(
                f"deployment {deployment} has no retained revision to roll back to"
            )

        routes = await self._select_routes(
            conn,
            org=org,
            project=row["project_id"],
            environment=str(row["environment"]),
            hostnames=targets,
        )
        route_changes = [
            route
            for route in routes
            if route["deployment_id"] != deployment
            or route["revision"] != row["revision"]
        ]

        current_live = await conn.fetch(
            "SELECT id FROM deployments "
            " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
            "   AND status = 'live' AND id <> $4",
            org,
            row["project_id"],
            row["git_ref"],
            deployment,
        )
        if not route_changes and not current_live and current == "live":
            return PromotionResult(deployment_from_row(row), 0, (), False)

        await self._audit_or_fail(
            AuditRecord(
                action="deployment.rollback",
                org_id=org,
                actor_type=actor_type,
                actor_id=actor_id,
                target_type="deployment",
                target_id=str(deployment),
                metadata={
                    "git_ref": str(row["git_ref"]),
                    "revision": str(row["revision"]),
                    "hostnames": [str(route["hostname"]) for route in routes],
                },
            )
        )

        superseded = await self._supersede_other_live(
            conn,
            org=org,
            project=row["project_id"],
            git_ref=str(row["git_ref"]),
            target_id=deployment,
        )
        updated = await self._cutover_routes(
            conn,
            org=org,
            project=row["project_id"],
            deployment=deployment_from_row(row),
            routes=route_changes,
            actor=actor,
        )

        if current != "live":
            # Resurrecting a superseded revision: its old terminal markers no
            # longer describe the row. The history of the move remains in
            # route_history and in the deployment that was just superseded.
            await conn.execute(
                "UPDATE deployments "
                "   SET status = 'live', finished_at = NULL, superseded_by = NULL "
                " WHERE id = $1 AND org_id = $2",
                deployment,
                org,
            )

        final = await self._load(conn, org, deployment)
        return PromotionResult(
            deployment_from_row(final),
            updated,
            superseded,
            changed=bool(updated or superseded or current != "live"),
        )

    # -------------------------------------------------------------- read side

    async def get(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        deployment_id: str | uuid.UUID,
    ) -> Deployment:
        org = _as_uuid(org_id, "org_id")
        deployment = _as_uuid(deployment_id, "deployment_id")
        return deployment_from_row(await self._load(conn, org, deployment))

    async def list_for_project(
        self,
        conn: asyncpg.Connection,
        *,
        org_id: str | uuid.UUID,
        project_id: str | uuid.UUID,
        limit: int = 50,
    ) -> list[Deployment]:
        org = _as_uuid(org_id, "org_id")
        project = _as_uuid(project_id, "project_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise InvalidDeployment("limit must be an integer between 1 and 500")
        rows = await conn.fetch(
            f"SELECT {_COLUMNS} FROM deployments "
            " WHERE org_id = $1 AND project_id = $2 "
            " ORDER BY created_at DESC LIMIT $3",
            org,
            project,
            limit,
        )
        return [deployment_from_row(row) for row in rows]

    # --------------------------------------------------------------- internals

    async def _require_project(
        self,
        conn: asyncpg.Connection,
        org: uuid.UUID,
        project: uuid.UUID,
    ) -> None:
        exists = await conn.fetchval(
            "SELECT 1 FROM projects WHERE id = $1 AND org_id = $2", project, org
        )
        if not exists:
            raise ProjectNotFound(
                f"project {project} is not visible in the caller's tenant"
            )

    async def _load(
        self,
        conn: asyncpg.Connection,
        org: uuid.UUID,
        deployment: uuid.UUID,
        *,
        for_update: bool = False,
    ) -> asyncpg.Record:
        sql = (
            f"SELECT {_COLUMNS} FROM deployments "
            " WHERE id = $1 AND org_id = $2"
        )
        if for_update:
            sql += " FOR UPDATE"
        row = await conn.fetchrow(sql, deployment, org)
        if row is None:
            raise DeploymentNotFound(
                f"deployment {deployment} is not visible in the caller's tenant"
            )
        return row

    async def _lock(self, conn: asyncpg.Connection, key: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", key
        )

    async def _newer_live(
        self,
        conn: asyncpg.Connection,
        *,
        org: uuid.UUID,
        project: uuid.UUID,
        git_ref: str,
        created_at: Any,
        exclude_id: uuid.UUID,
    ) -> uuid.UUID | None:
        return await conn.fetchval(
            "SELECT id FROM deployments "
            " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
            "   AND id <> $4 AND status = 'live' "
            "   AND created_at > $5 "
            " ORDER BY created_at DESC LIMIT 1",
            org,
            project,
            git_ref,
            exclude_id,
            created_at,
        )

    async def _supersede_older(
        self,
        conn: asyncpg.Connection,
        *,
        org: uuid.UUID,
        project: uuid.UUID,
        git_ref: str,
        created_before: Any,
        exclude_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        rows = await conn.fetch(
            "UPDATE deployments "
            "   SET status = 'superseded', finished_at = now(), "
            "       superseded_by = $5 "
            " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
            "   AND id <> $5 AND status IN ('ready', 'live') "
            "   AND created_at < $4 "
            "RETURNING id",
            org,
            project,
            git_ref,
            created_before,
            exclude_id,
        )
        return tuple(row["id"] for row in rows)

    async def _supersede_other_live(
        self,
        conn: asyncpg.Connection,
        *,
        org: uuid.UUID,
        project: uuid.UUID,
        git_ref: str,
        target_id: uuid.UUID,
    ) -> tuple[uuid.UUID, ...]:
        rows = await conn.fetch(
            "UPDATE deployments "
            "   SET status = 'superseded', finished_at = now(), "
            "       superseded_by = $4 "
            " WHERE org_id = $1 AND project_id = $2 AND git_ref = $3 "
            "   AND id <> $4 AND status = 'live' "
            "RETURNING id",
            org,
            project,
            git_ref,
            target_id,
        )
        return tuple(row["id"] for row in rows)

    async def _select_routes(
        self,
        conn: asyncpg.Connection,
        *,
        org: uuid.UUID,
        project: uuid.UUID,
        environment: str,
        hostnames: Sequence[str] | None,
    ) -> list[asyncpg.Record]:
        if hostnames is not None:
            rows = await conn.fetch(
                "SELECT r.id, r.hostname, r.path_prefix, r.kind::text AS kind, "
                "       r.deployment_id, r.revision, "
                "       d.environment AS current_environment "
                "  FROM routes r "
                "  LEFT JOIN deployments d "
                "    ON d.id = r.deployment_id "
                "   AND d.org_id = r.org_id AND d.project_id = r.project_id "
                " WHERE r.org_id = $1 AND r.project_id = $2 "
                "   AND r.hostname = ANY($3::text[]) "
                " ORDER BY r.hostname, r.path_prefix "
                " FOR UPDATE OF r",
                org,
                project,
                list(hostnames),
            )
            present = {str(row["hostname"]) for row in rows}
            missing = [name for name in hostnames if name not in present]
            if missing:
                raise NoRoute(
                    "no route rows for hostname(s) "
                    + ", ".join(sorted(missing))
                    + "; allocate them in SPEC-15 first"
                )
        else:
            rows = await conn.fetch(
                "SELECT r.id, r.hostname, r.path_prefix, r.kind::text AS kind, "
                "       r.deployment_id, r.revision, "
                "       d.environment AS current_environment "
                "  FROM routes r "
                "  JOIN deployments d ON d.id = r.deployment_id "
                " WHERE r.org_id = $1 AND r.project_id = $2 "
                "   AND d.org_id = $1 AND d.project_id = $2 "
                "   AND d.environment = $3 "
                " ORDER BY r.hostname, r.path_prefix "
                " FOR UPDATE OF r",
                org,
                project,
                environment,
            )

        # Redirect/rewrite/notfound rows are not revisions of the app and never
        # carry the deployment pointer. SPEC-15's apex redirect is one of them,
        # so a requested hostname may legitimately have only such a row.
        selected = [row for row in rows if row["kind"] in {"proxy", "static"}]
        if not selected:
            raise NoRoute(
                "no serving route is available for this cutover; "
                "allocate the hostname in SPEC-15 first"
            )
        for route in selected:
            current_environment = route["current_environment"]
            if current_environment is not None and str(current_environment) != environment:
                raise InvalidDeployment(
                    f"route {route['hostname']!r} serves a different environment"
                )
        return selected

    async def _cutover_routes(
        self,
        conn: asyncpg.Connection,
        *,
        org: uuid.UUID,
        project: uuid.UUID,
        deployment: Deployment,
        routes: Sequence[asyncpg.Record],
        actor: str,
    ) -> int:
        updated = 0
        for route in routes:
            result = await conn.execute(
                "UPDATE routes "
                "   SET deployment_id = $1, revision = $2, updated_at = now() "
                " WHERE id = $3 AND org_id = $4 AND project_id = $5",
                deployment.id,
                deployment.revision,
                route["id"],
                org,
                project,
            )
            if result != "UPDATE 1":
                raise DeploymentNotFound(
                    f"route {route['hostname']!r} disappeared during cutover"
                )
            await conn.execute(
                "INSERT INTO route_history "
                "  (hostname, from_revision, to_revision, deployment_id, actor) "
                "VALUES ($1, $2, $3, $4, $5)",
                route["hostname"],
                route["revision"],
                deployment.revision,
                deployment.id,
                actor,
            )
            updated += 1
        return updated

    async def _cancel_jobs(
        self,
        conn: asyncpg.Connection,
        org: uuid.UUID,
        deployment_ids: Sequence[uuid.UUID],
    ) -> None:
        if not deployment_ids:
            return
        await conn.execute(
            "UPDATE build_jobs "
            "   SET status = 'cancelled', updated_at = now() "
            " WHERE org_id = $1 AND deployment_id = ANY($2::uuid[]) "
            "   AND status IN ('pending', 'running')",
            org,
            list(deployment_ids),
        )

    async def _audit_or_fail(self, record: AuditRecord) -> None:
        try:
            await self._audit.write(record)
        except Exception as exc:  # boundary: no value-bearing driver error leaks
            raise AuditUnavailable(
                f"audit write failed for {record.action}; operation refused"
            ) from exc


# ---------------------------------------------------------------- validation


def _as_uuid(value: str | uuid.UUID, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InvalidDeployment(f"{field} must be a UUID") from exc


def _validate_stage(stage: str | None) -> str | None:
    if stage is None:
        return None
    if not isinstance(stage, str) or not stage.strip() or len(stage) > 100:
        raise InvalidDeployment("stage must be a non-empty string of at most 100 chars")
    return stage.strip()


def _validate_message(message: str | None) -> str | None:
    if message is None:
        return None
    if not isinstance(message, str) or "\x00" in message:
        raise InvalidDeployment("message must be a string without NUL")
    return message


__all__ = ["DeploymentLifecycle"]
