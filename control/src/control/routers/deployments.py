"""Deployment trigger, status, cancellation, promotion and log SSE.

The control plane records intent and returns immediately (SPEC-04 R-4). Build
execution belongs to the build worker; Kubernetes mutation belongs to the
runtime reconcilers. This module deliberately does neither.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import unquote, urlparse

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import (
    CancelOut,
    DeploymentListOut,
    DeploymentOut,
    DeploymentTriggerIn,
    DeploymentTriggerOut,
    PromoteIn,
)
from control.pagination import clamp_limit, decode_cursor, next_cursor
from control.security import (
    ALL_ROLES,
    AuthContext,
    get_auth,
    require_admin,
    resolve_deployment,
    resolve_project,
)
from control.settings import Settings, get_settings

router = APIRouter(tags=["deployments"])
Auth = Annotated[AuthContext, Depends(get_auth)]

_TERMINAL = {"failed", "cancelled", "superseded"}
_STATUSES = {"queued", "building", "ready", "live", "failed", "cancelled", "superseded"}


def _deployment_out(row) -> DeploymentOut:
    return DeploymentOut(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        status=row["status"],
        stage=row["stage"],
        git_sha=row["git_sha"],
        git_ref=row["git_ref"],
        environment=row["environment"],
        trigger_source=row["trigger_source"],
        revision=row["revision"],
        image_digest=row["image_digest"],
        manifest_hash=row["manifest_hash"],
        preview_hostname=row["preview_hostname"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        exit_code=row["exit_code"],
        error_message=row["error_message"],
        log_uri=row["log_uri"],
    )


async def _audit(
    request: Request,
    *,
    action: str,
    auth: AuthContext,
    org_id: str,
    target_type: str,
    target_id: str,
    metadata: dict | None = None,
) -> None:
    db: db_module.Db = request.app.state.db
    try:
        await db_module.write_audit(
            db,
            action=action,
            actor_type="user",
            actor_id=auth.user_id,
            org_id=org_id,
            target_type=target_type,
            target_id=target_id,
            metadata=metadata or {},
        )
    except Exception as exc:  # noqa: BLE001 - audit is a precondition, not a best effort
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "the audit log is unavailable; the action was not performed",
            headers={"Retry-After": "5"},
        ) from exc


@router.post(
    "/projects/{project_id}/deployments",
    response_model=DeploymentTriggerOut,
    status_code=202,
)
async def trigger_deployment(
    request: Request,
    project_id: str,
    body: DeploymentTriggerIn,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> DeploymentTriggerOut:
    access = await resolve_project(request, auth, project_id)
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")

    if body.environment not in {"production", "preview"}:
        raise ApiProblem(
            422,
            "validation_failed",
            "environment must be production or preview",
            fields={"environment": "invalid environment"},
        )

    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        project = await conn.fetchrow(
            "SELECT id, org_id, name, slug, provider_repo_id, default_branch "
            "FROM projects WHERE id = $1 AND org_id = $2",
            uuid.UUID(project_id),
            uuid.UUID(access.org_id),
        )
        if project is None:
            raise not_found("project")
        git_ref = body.git_ref or project["default_branch"]
        git_sha = body.git_sha
        if not git_sha:
            if project["provider_repo_id"] <= 0:
                # Local fixture projects have no provider commit yet. The sha
                # is stable for the project/ref pair so repeated local builds
                # exercise the duplicate-delivery path honestly.
                git_sha = hashlib.sha1(
                    f"{project_id}:{git_ref}".encode("utf-8")
                ).hexdigest()
            else:
                raise ApiProblem(
                    422,
                    "validation_failed",
                    "a connected repository needs the webhook commit sha; "
                    "manual HEAD resolution is not implemented yet",
                    fields={"git_sha": "required for a connected repository"},
                )
        manifest = None
        manifest_hash = None
        deployment_id = uuid.uuid4()
        await _audit(
            request,
            action="deploy.trigger",
            auth=auth,
            org_id=access.org_id,
            target_type="project",
            target_id=project_id,
            metadata={"deployment_id": str(deployment_id), "git_ref": git_ref},
        )
        row = await conn.fetchrow(
            """
            INSERT INTO deployments
                (id, project_id, org_id, git_sha, git_ref, environment,
                 triggered_by, trigger_source, status, manifest, manifest_hash,
                 build_image, build_env_keys)
            VALUES ($1, $2, $3, $4, $5, $6, $7, 'manual', 'queued',
                    $8::jsonb, $9, $10, '{}'::text[])
            RETURNING *
            """,
            deployment_id,
            uuid.UUID(project_id),
            uuid.UUID(access.org_id),
            git_sha,
            git_ref,
            body.environment,
            uuid.UUID(auth.user_id),
            json.dumps(manifest) if manifest is not None else None,
            manifest_hash,
            settings.default_build_image,
        )
        await conn.execute(
            """
            INSERT INTO build_jobs (deployment_id, org_id, project_id)
            VALUES ($1, $2, $3)
            """,
            deployment_id,
            uuid.UUID(access.org_id),
            uuid.UUID(project_id),
        )
        position = await conn.fetchval(
            """
            SELECT count(*) FROM build_jobs
             WHERE org_id = $1 AND status = 'pending'
            """,
            uuid.UUID(access.org_id),
        )
    out = DeploymentTriggerOut(
        **_deployment_out(row).model_dump(),
        queue_position=int(position or 0),
    )
    return out


@router.get("/projects/{project_id}/deployments", response_model=DeploymentListOut)
async def list_deployments(
    request: Request,
    project_id: str,
    auth: Auth,
    env: str | None = None,
    status: str | None = None,
    cursor: str | None = None,
    limit: int = 25,
) -> DeploymentListOut:
    access = await resolve_project(request, auth, project_id)
    if env is not None and env not in {"production", "preview"}:
        raise ApiProblem(
            422,
            "validation_failed",
            "env must be production or preview",
            fields={"env": "invalid environment"},
        )
    if status is not None and status not in _STATUSES:
        raise ApiProblem(
            422,
            "validation_failed",
            "unknown deployment status",
            fields={"status": "invalid status"},
        )
    db: db_module.Db = request.app.state.db
    take = clamp_limit(limit)
    params: list[object] = [uuid.UUID(project_id), uuid.UUID(access.org_id)]
    where = ["project_id = $1", "org_id = $2"]
    if env is not None:
        params.append(env)
        where.append(f"environment = ${len(params)}")
    if status is not None:
        params.append(status)
        where.append(f"status = ${len(params)}::deployment_status")
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        params.extend([created_at, row_id])
        where.append(f"(created_at, id) < (${len(params)-1}, ${len(params)})")
    params.append(take + 1)
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM deployments
             WHERE {' AND '.join(where)}
             ORDER BY created_at DESC, id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    return DeploymentListOut(
        items=[_deployment_out(row) for row in rows[:take]],
        next_cursor=next_cursor(rows, take),
    )


@router.get("/deployments/{deployment_id}", response_model=DeploymentOut)
async def deployment_detail(
    request: Request, deployment_id: str, auth: Auth
) -> DeploymentOut:
    access = await resolve_deployment(request, auth, deployment_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deployments WHERE id = $1 AND org_id = $2",
            uuid.UUID(deployment_id),
            uuid.UUID(access.org_id),
        )
    if row is None:
        raise not_found("deployment")
    return _deployment_out(row)


@router.post("/deployments/{deployment_id}/cancel", response_model=CancelOut)
async def cancel_deployment(
    request: Request, deployment_id: str, auth: Auth
) -> CancelOut:
    access = await resolve_deployment(request, auth, deployment_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM deployments WHERE id = $1 AND org_id = $2 FOR UPDATE",
            uuid.UUID(deployment_id),
            uuid.UUID(access.org_id),
        )
        if row is None:
            raise not_found("deployment")
        status = row["status"]
        if status in _TERMINAL or status in {"ready", "live"}:
            raise ApiProblem(
                409,
                "conflict",
                f"deployment is already {status} and cannot be cancelled",
            )
        await _audit(
            request,
            action="deploy.cancel",
            auth=auth,
            org_id=access.org_id,
            target_type="deployment",
            target_id=deployment_id,
        )
        if status == "queued":
            await conn.execute(
                "UPDATE build_jobs SET status = 'cancelled', updated_at = now() "
                "WHERE deployment_id = $1 AND status = 'pending'",
                uuid.UUID(deployment_id),
            )
            updated = await conn.fetchrow(
                "UPDATE deployments SET status = 'cancelled', stage = 'cancelled', "
                "finished_at = now() WHERE id = $1 RETURNING *",
                uuid.UUID(deployment_id),
            )
            outcome = "cancelled"
            detail = "the queued deployment was cancelled before a worker claimed it"
        else:
            updated = await conn.fetchrow(
                "UPDATE deployments SET stage = 'cancel_requested' "
                "WHERE id = $1 RETURNING *",
                uuid.UUID(deployment_id),
            )
            outcome = "cancellation_requested"
            detail = (
                "the build is running; cancellation was requested and the worker "
                "will stop at its next checkpoint"
            )
    return CancelOut(
        deployment=_deployment_out(updated),
        outcome=outcome,
        detail=detail,
    )


@router.post("/deployments/{deployment_id}/promote", response_model=DeploymentOut)
async def promote_deployment(
    request: Request,
    deployment_id: str,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
    body: PromoteIn | None = None,
) -> DeploymentOut:
    access = await resolve_deployment(request, auth, deployment_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    await _audit(
        request,
        action="deploy.promote",
        auth=auth,
        org_id=access.org_id,
        target_type="deployment",
        target_id=deployment_id,
    )
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        deployment = await conn.fetchrow(
            "SELECT * FROM deployments WHERE id = $1 AND org_id = $2 FOR UPDATE",
            uuid.UUID(deployment_id),
            uuid.UUID(access.org_id),
        )
        if deployment is None:
            raise not_found("deployment")
        if deployment["status"] not in {"ready", "live"}:
            raise ApiProblem(
                409,
                "conflict",
                f"deployment is {deployment['status']}; only a ready deployment can be promoted",
            )
        revision = (body.revision if body else None) or deployment["revision"]
        if not revision:
            raise ApiProblem(
                409,
                "conflict",
                "deployment has no image revision to promote",
            )
        if body and body.revision and deployment["revision"] != body.revision:
            raise ApiProblem(
                409,
                "conflict",
                "the requested revision is not the one this deployment built",
            )
        project = await conn.fetchrow(
            "SELECT id, slug FROM projects WHERE id = $1 AND org_id = $2",
            uuid.UUID(access.project_id),
            uuid.UUID(access.org_id),
        )
        if project is None:
            raise not_found("project")
        route = await conn.fetchrow(
            "SELECT * FROM routes WHERE project_id = $1 ORDER BY priority DESC, created_at LIMIT 1 FOR UPDATE",
            uuid.UUID(access.project_id),
        )
        from_revision = None
        if route is None:
            hostname = f"{project['slug']}.{settings.apps_domain}"
            taken = await conn.fetchval(
                "SELECT 1 FROM routes WHERE hostname = $1", hostname
            )
            if taken:
                hostname = (
                    f"{project['slug']}-{uuid.uuid4().hex[:6]}.{settings.apps_domain}"
                )
            route = await conn.fetchrow(
                """
                INSERT INTO routes
                    (org_id, project_id, hostname, deployment_id, revision)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                uuid.UUID(access.org_id),
                uuid.UUID(access.project_id),
                hostname,
                uuid.UUID(deployment_id),
                revision,
            )
        else:
            from_revision = route["revision"]
            await conn.execute(
                "UPDATE deployments SET status = 'ready' "
                "WHERE project_id = $1 AND status = 'live' AND id <> $2",
                uuid.UUID(access.project_id),
                uuid.UUID(deployment_id),
            )
            await conn.execute(
                """
                UPDATE routes
                   SET deployment_id = $1, revision = $2, updated_at = now()
                 WHERE id = $3 AND org_id = $4
                """,
                uuid.UUID(deployment_id),
                revision,
                route["id"],
                uuid.UUID(access.org_id),
            )
        await conn.execute(
            """
            INSERT INTO route_history
                (hostname, from_revision, to_revision, deployment_id, actor)
            VALUES ($1, $2, $3, $4, $5)
            """,
            route["hostname"],
            from_revision,
            revision,
            uuid.UUID(deployment_id),
            f"user:{auth.user_id}",
        )
        updated = await conn.fetchrow(
            "UPDATE deployments SET status = 'live', stage = 'live' "
            "WHERE id = $1 RETURNING *",
            uuid.UUID(deployment_id),
        )
    return _deployment_out(updated)


@router.get("/deployments/{deployment_id}/graph")
async def deployment_graph(
    request: Request, deployment_id: str, auth: Auth
) -> dict:
    access = await resolve_deployment(request, auth, deployment_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT manifest FROM deployments WHERE id = $1 AND org_id = $2",
            uuid.UUID(deployment_id),
            uuid.UUID(access.org_id),
        )
    if row is None:
        raise not_found("deployment")
    manifest = row["manifest"] or {}
    services = manifest.get("services") or []
    nodes = [
        {"id": service.get("name", f"service-{i}"), "kind": "service"}
        for i, service in enumerate(services)
        if isinstance(service, dict)
    ]
    return {"nodes": nodes, "edges": []}


def _read_log_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def _log_path(log_uri: str) -> Path | None:
    parsed = urlparse(log_uri)
    if parsed.scheme != "file":
        return None
    raw = unquote(parsed.path)
    # file:///C:/... on Windows; urlparse keeps the leading slash.
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


async def _log_events(
    path: Path,
    start: int,
    *,
    db: db_module.Db,
    org_id: str,
    deployment_id: str,
):
    yield "retry: 3000\n\n"
    emitted = max(start, 0)
    idle_ticks = 0
    while True:
        lines = await asyncio.to_thread(_read_log_lines, path)
        while emitted < len(lines):
            emitted += 1
            payload = json.dumps(
                {"seq": emitted, "line": lines[emitted - 1]},
                separators=(",", ":"),
            )
            yield f"id: {emitted}\nevent: log\ndata: {payload}\n\n"
        async with db_module.tenant_scope(db, org_id) as conn:
            status = await conn.fetchval(
                "SELECT status FROM deployments WHERE id = $1 AND org_id = $2",
                uuid.UUID(deployment_id),
                uuid.UUID(org_id),
            )
        if status in _TERMINAL or status in {"ready", "live"}:
            if emitted >= len(lines):
                yield "event: end\ndata: {\"status\":\"" + str(status) + "\"}\n\n"
                return
        idle_ticks += 1
        if idle_ticks > 1800:
            yield "event: end\ndata: {\"status\":\"timeout\"}\n\n"
            return
        await asyncio.sleep(1)


@router.get("/deployments/{deployment_id}/logs")
async def deployment_logs(
    request: Request,
    deployment_id: str,
    auth: Auth,
    from_: int = Query(0, alias="from", ge=0),
):
    access = await resolve_deployment(request, auth, deployment_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT log_uri FROM deployments WHERE id = $1 AND org_id = $2",
            uuid.UUID(deployment_id),
            uuid.UUID(access.org_id),
        )
    if row is None:
        raise not_found("deployment")
    if not row["log_uri"]:
        raise ApiProblem(
            409,
            "conflict",
            "the build has not opened a log stream yet",
        )
    path = _log_path(row["log_uri"])
    if path is None:
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "the configured log store is not readable by this process",
        )
    return StreamingResponse(
        _log_events(
            path,
            from_,
            db=db,
            org_id=access.org_id,
            deployment_id=deployment_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
