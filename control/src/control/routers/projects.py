"""Project CRUD and the project detail aggregate (SPEC-36 §3.2)."""

from __future__ import annotations

import re
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Query, Request

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import (
    BranchOut,
    ConnectedRepoOut,
    DeploymentOut,
    DnsRecordOut,
    DomainOut,
    ProjectCreateIn,
    ProjectDetailOut,
    ProjectListOut,
    ProjectOut,
    ProjectPatchIn,
    SecretOut,
)
from control.pagination import clamp_limit, decode_cursor, next_cursor
from control.security import (
    AuthContext,
    get_auth,
    require_admin,
    resolve_org,
    resolve_project,
)

router = APIRouter(prefix="/projects", tags=["projects"])
Auth = Annotated[AuthContext, Depends(get_auth)]


def _project_out(row) -> ProjectOut:
    provider = row["provider"]
    repo_id = row["provider_repo_id"]
    connected = ConnectedRepoOut(
        provider=provider,
        provider_repo_id=repo_id,
        full_name=f"{provider}:{repo_id}" if repo_id > 0 else "local fixture",
    )
    keys = set(row.keys())
    environments = (
        list(row["environments"])
        if "environments" in keys and row["environments"] is not None
        else []
    )
    last_deployment = None
    if "last_deployment" in keys and row["last_deployment"] is not None:
        last_deployment = DeploymentOut.model_validate(row["last_deployment"])
    return ProjectOut(
        id=str(row["id"]),
        org=row["org_slug"],
        name=row["name"],
        slug=row["slug"],
        provider=provider,
        provider_repo_id=repo_id,
        default_branch=row["default_branch"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        connected_repo=connected,
        environments=environments,
        last_deployment=last_deployment,
        root_path=row["root_path"] if "root_path" in keys else None,
    )


_LATEST_DEPLOYMENT = """
    (SELECT row_to_json(d)
       FROM (
         SELECT d.id::text AS id,
                d.project_id::text AS project_id,
                d.status::text AS status,
                d.stage,
                d.git_sha,
                d.git_ref,
                d.environment,
                d.trigger_source,
                d.revision,
                d.image_digest,
                d.manifest_hash,
                d.preview_hostname,
                d.created_at,
                d.started_at,
                d.finished_at,
                d.exit_code,
                d.error_message,
                d.log_uri
           FROM deployments d
          WHERE d.project_id = p.id
          ORDER BY d.created_at DESC
          LIMIT 1
       ) d
    ) AS last_deployment
"""

_ENVIRONMENTS = """
    COALESCE(
      (SELECT array_agg(DISTINCT b.environment)
         FROM project_branches b
        WHERE b.project_id = p.id),
      ARRAY[]::text[]
    ) AS environments
"""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "project")[:48].strip("-") or "project"


@router.get("", response_model=ProjectListOut)
async def list_projects(
    request: Request,
    auth: Auth,
    org: str = Query(..., min_length=1),
    cursor: str | None = None,
    limit: int = 25,
) -> ProjectListOut:
    access = await resolve_org(request, auth, org)
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    take = clamp_limit(limit)
    params: list[object] = [uuid.UUID(access.org_id)]
    where = ["p.org_id = $1", "p.status = 'active'"]
    if cursor:
        created_at, row_id = decode_cursor(cursor)
        params.extend([created_at, row_id])
        where.append("(p.created_at, p.id) < ($2, $3)")
    params.append(take + 1)
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await conn.fetch(
            f"""
            SELECT p.id, p.org_id, p.name, p.slug, p.provider,
                   p.provider_repo_id, p.default_branch, p.status,
                   p.created_at, p.updated_at, o.slug AS org_slug,
                   {_ENVIRONMENTS},
                   {_LATEST_DEPLOYMENT}
              FROM projects p
              JOIN orgs o ON o.id = p.org_id
             WHERE {' AND '.join(where)}
             ORDER BY p.created_at DESC, p.id DESC
             LIMIT ${len(params)}
            """,
            *params,
        )
    items = [_project_out(row) for row in rows[:take]]
    return ProjectListOut(items=items, next_cursor=next_cursor(rows, take))


@router.post("", response_model=ProjectDetailOut, status_code=201)
async def create_project(
    request: Request,
    body: ProjectCreateIn,
    auth: Auth,
) -> ProjectDetailOut:
    access = await resolve_org(request, auth, body.org)
    require_admin(access)
    name = body.name.strip()
    if not name:
        raise ApiProblem(
            422,
            "validation_failed",
            "name is required",
            fields={"name": "required"},
        )
    root_path = (body.root_path or "/").strip() or "/"
    if not root_path.startswith("/"):
        raise ApiProblem(
            422,
            "validation_failed",
            "root_path must be absolute",
            fields={"root_path": "must begin with /"},
        )
    slug = _slugify(name)
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    project_id = uuid.uuid4()
    namespace = f"org-{uuid.UUID(access.org_id).hex[:12]}"
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        # A readable suffix keeps two projects with the same display name from
        # colliding without making the client choose a slug.
        candidate = slug
        for attempt in range(20):
            exists = await conn.fetchval(
                "SELECT 1 FROM projects WHERE org_id = $1 AND slug = $2",
                uuid.UUID(access.org_id),
                candidate,
            )
            if not exists:
                break
            candidate = f"{slug[:43]}-{uuid.uuid4().hex[:6]}"
        else:
            raise ApiProblem(409, "conflict", "could not allocate a project slug")
        await conn.execute(
            """
            INSERT INTO projects
                (id, org_id, name, slug, provider, provider_repo_id,
                 default_branch, k8s_namespace, root_path)
            VALUES ($1, $2, $3, $4, 'github', 0, $5, $6, $7)
            """,
            project_id,
            uuid.UUID(access.org_id),
            name,
            candidate,
            body.default_branch or "main",
            namespace,
            root_path,
        )
        await conn.execute(
            """
            INSERT INTO project_branches (project_id, org_id, git_ref, environment)
            VALUES ($1, $2, $3, 'production')
            ON CONFLICT (project_id, git_ref) DO NOTHING
            """,
            project_id,
            uuid.UUID(access.org_id),
            body.default_branch or "main",
        )
    return await project_detail(request, str(project_id), auth)


@router.get("/{project_id}", response_model=ProjectDetailOut)
async def project_detail(
    request: Request, project_id: str, auth: Auth
) -> ProjectDetailOut:
    access = await resolve_project(request, auth, project_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            f"""
            SELECT p.*, o.slug AS org_slug,
                   {_ENVIRONMENTS},
                   {_LATEST_DEPLOYMENT}
              FROM projects p JOIN orgs o ON o.id = p.org_id
             WHERE p.id = $1 AND p.org_id = $2
            """,
            uuid.UUID(project_id),
            uuid.UUID(access.org_id),
        )
        if row is None:
            raise not_found("project")
        branches = await conn.fetch(
            "SELECT git_ref, environment, auto_deploy FROM project_branches "
            "WHERE project_id = $1 ORDER BY position, git_ref",
            uuid.UUID(project_id),
        )
        domains = await conn.fetch(
            "SELECT id, project_id, hostname, is_production, status, "
            "verification_token, cert_issued_at, cert_expires_at "
            "FROM domains WHERE project_id = $1 ORDER BY created_at DESC",
            uuid.UUID(project_id),
        )
        secrets = await conn.fetch(
            "SELECT key, target, environments, last_used_at FROM env_vars "
            "WHERE project_id = $1 ORDER BY key",
            uuid.UUID(project_id),
        )
    return ProjectDetailOut(
        **_project_out(row).model_dump(),
        branches=[
            BranchOut(
                git_ref=branch["git_ref"],
                environment=branch["environment"],
                auto_deploy=branch["auto_deploy"],
            )
            for branch in branches
        ],
        domains=[
            DomainOut(
                id=str(domain["id"]),
                project_id=str(domain["project_id"]),
                hostname=domain["hostname"],
                status=domain["status"],
                is_production=domain["is_production"],
                verification_token=domain["verification_token"],
                cert_issued_at=domain["cert_issued_at"],
                cert_expires_at=domain["cert_expires_at"],
                dns_instructions=[
                    DnsRecordOut(
                        type="TXT",
                        name=f"_autopilot.{domain['hostname']}",
                        value=domain["verification_token"],
                    )
                ],
            )
            for domain in domains
        ],
        secrets=[
            SecretOut(
                key=secret["key"],
                target=secret["target"],
                environments=list(secret["environments"]),
                last_used_at=secret["last_used_at"],
            )
            for secret in secrets
        ],
    )


@router.patch("/{project_id}", response_model=ProjectDetailOut)
async def patch_project(
    request: Request, project_id: str, body: ProjectPatchIn, auth: Auth
) -> ProjectDetailOut:
    access = await resolve_project(request, auth, project_id)
    require_admin(access)
    if body.root_path is not None and not body.root_path.strip().startswith("/"):
        raise ApiProblem(
            422,
            "validation_failed",
            "root_path must be absolute",
            fields={"root_path": "must begin with /"},
        )
    if body.branches is not None:
        seen: set[str] = set()
        for branch in body.branches:
            if not branch.git_ref.strip():
                raise ApiProblem(
                    422,
                    "validation_failed",
                    "branch git ref must not be empty",
                    fields={"branches.git_ref": "required"},
                )
            if branch.environment not in {"production", "preview"}:
                raise ApiProblem(
                    422,
                    "validation_failed",
                    "branch environment must be production or preview",
                    fields={"branches.environment": "invalid environment"},
                )
            if branch.git_ref in seen:
                raise ApiProblem(
                    422,
                    "validation_failed",
                    "branch rules must have unique git refs",
                    fields={"branches.git_ref": "duplicate"},
                )
            seen.add(branch.git_ref)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            """
            UPDATE projects
               SET name = COALESCE($3, name),
                   default_branch = COALESCE($4, default_branch),
                   root_path = COALESCE($5, root_path),
                   updated_at = now()
             WHERE id = $1 AND org_id = $2
             RETURNING id
            """,
            uuid.UUID(project_id),
            uuid.UUID(access.org_id),
            body.name.strip() if body.name else None,
            body.default_branch,
            body.root_path.strip() if body.root_path is not None else None,
        )
        if row is None:
            raise not_found("project")
        if body.branches is not None:
            await conn.execute(
                "DELETE FROM project_branches WHERE project_id = $1 AND org_id = $2",
                uuid.UUID(project_id),
                uuid.UUID(access.org_id),
            )
            for position, branch in enumerate(body.branches):
                await conn.execute(
                    """
                    INSERT INTO project_branches
                        (project_id, org_id, git_ref, environment, auto_deploy, position)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    uuid.UUID(project_id),
                    uuid.UUID(access.org_id),
                    branch.git_ref,
                    branch.environment,
                    branch.auto_deploy,
                    position,
                )
        elif body.branch_environments:
            for ref, environment in body.branch_environments.items():
                if environment not in {"production", "preview"}:
                    raise ApiProblem(
                        422,
                        "validation_failed",
                        "branch environment must be production or preview",
                        fields={f"branch_environments.{ref}": "invalid environment"},
                    )
                await conn.execute(
                    """
                    INSERT INTO project_branches
                        (project_id, org_id, git_ref, environment, auto_deploy)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (project_id, git_ref) DO UPDATE
                        SET environment = EXCLUDED.environment,
                            auto_deploy = EXCLUDED.auto_deploy
                    """,
                    uuid.UUID(project_id),
                    uuid.UUID(access.org_id),
                    ref,
                    environment,
                    body.auto_deploy if body.auto_deploy is not None else True,
                )
        elif body.auto_deploy is not None:
            await conn.execute(
                "UPDATE project_branches SET auto_deploy = $3 "
                "WHERE project_id = $1 AND org_id = $2",
                uuid.UUID(project_id),
                uuid.UUID(access.org_id),
                body.auto_deploy,
            )
    return await project_detail(request, project_id, auth)


async def _org_slug(request: Request, org_id: str) -> str:
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, org_id) as conn:
        return await conn.fetchval("SELECT slug FROM orgs WHERE id = $1", uuid.UUID(org_id))


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    request: Request, project_id: str, auth: Auth
) -> None:
    access = await resolve_project(request, auth, project_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        # Soft-delete is represented by suspension until SPEC-03 grows a
        # deleted_at column; the child rows remain recoverable for support.
        result = await conn.execute(
            "UPDATE projects SET status = 'suspended', updated_at = now() "
            "WHERE id = $1 AND org_id = $2",
            uuid.UUID(project_id),
            uuid.UUID(access.org_id),
        )
        if result != "UPDATE 1":
            raise not_found("project")
