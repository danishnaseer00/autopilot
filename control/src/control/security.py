"""Authentication and pre-tenant authorization (SPEC-05).

The token is opaque and only its SHA-256 digest reaches Postgres. Every
resource lookup before tenant context goes through a narrow SECURITY DEFINER
function; once the org id is known, the request runs under `tenant_scope`, so
RLS is still the backstop for the actual read/write.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Literal

from fastapi import Request, Response

from control import db as db_module
from control.errors import ApiProblem, forbidden, not_found, unauthenticated
from control.settings import Settings

Role = Literal["owner", "admin", "member"]
ALL_ROLES: tuple[Role, ...] = ("owner", "admin", "member")
ADMIN_ROLES: tuple[Role, ...] = ("owner", "admin")


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8")).digest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class AuthContext:
    session_id: str
    user_id: str
    email: str
    display_name: str
    is_platform_admin: bool


@dataclass(frozen=True, slots=True)
class OrgAccess:
    org_id: str
    slug: str
    name: str
    role: Role
    status: str
    plan_code: str | None
    plan_name: str | None
    plan_limits: dict
    subscription_status: str | None
    current_period_end: datetime | None


@dataclass(frozen=True, slots=True)
class ResourceAccess:
    org_id: str
    role: Role
    resource_id: str
    project_id: str | None = None


def _pool(request: Request):
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "the database is not configured",
            headers={"Retry-After": "5"},
        )
    return db


async def get_auth(request: Request) -> AuthContext:
    settings: Settings = request.app.state.settings
    token = request.cookies.get(settings.session_cookie_name)
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise unauthenticated()

    db = _pool(request)
    row = await db.pool.fetchrow(
        "SELECT * FROM auth_lookup_session($1)", hash_token(token)
    )
    if row is None:
        raise unauthenticated()

    await db.pool.execute(
        "SELECT auth_touch_session($1, $2)",
        row["session_id"],
        max(settings.session_idle_hours, 1) * 3600,
    )
    return AuthContext(
        session_id=str(row["session_id"]),
        user_id=str(row["user_id"]),
        email=row["email"],
        display_name=row["display_name"],
        is_platform_admin=bool(row["is_platform_admin"]),
    )


async def _org_rows(request: Request, user_id: str) -> list:
    db = _pool(request)
    return await db.pool.fetch("SELECT * FROM auth_list_user_orgs($1)", user_id)


def _org_access(row) -> OrgAccess:
    return OrgAccess(
        org_id=str(row["org_id"]),
        slug=row["slug"],
        name=row["name"],
        role=row["role"],
        status=row["org_status"],
        plan_code=row["plan_code"],
        plan_name=row["plan_name"],
        plan_limits=dict(row["plan_limits"] or {}),
        subscription_status=row["subscription_status"],
        current_period_end=row["current_period_end"],
    )


async def list_orgs(request: Request, auth: AuthContext) -> list[OrgAccess]:
    return [_org_access(row) for row in await _org_rows(request, auth.user_id)]


async def resolve_org(request: Request, auth: AuthContext, slug: str) -> OrgAccess:
    for org in await list_orgs(request, auth):
        if org.slug == slug:
            if org.status == "suspended":
                raise ApiProblem(
                    403,
                    "tenant_suspended",
                    "this organization is suspended; contact support",
                )
            return org
    raise not_found("organization")


async def resolve_project(
    request: Request, auth: AuthContext, project_id: str
) -> ResourceAccess:
    db = _pool(request)
    try:
        project_uuid = uuid.UUID(project_id)
    except ValueError as exc:
        raise not_found("project") from exc
    row = await db.pool.fetchrow(
        "SELECT * FROM auth_project_access($1, $2)", auth.user_id, project_uuid
    )
    if row is None:
        raise not_found("project")
    return ResourceAccess(
        org_id=str(row["org_id"]),
        role=row["role"],
        resource_id=str(row["project_id"]),
    )


async def resolve_deployment(
    request: Request, auth: AuthContext, deployment_id: str
) -> ResourceAccess:
    db = _pool(request)
    try:
        deployment_uuid = uuid.UUID(deployment_id)
    except ValueError as exc:
        raise not_found("deployment") from exc
    row = await db.pool.fetchrow(
        "SELECT * FROM auth_deployment_access($1, $2)",
        auth.user_id,
        deployment_uuid,
    )
    if row is None:
        raise not_found("deployment")
    return ResourceAccess(
        org_id=str(row["org_id"]),
        role=row["role"],
        resource_id=str(row["deployment_id"]),
        project_id=str(row["project_id"]),
    )


async def resolve_domain(
    request: Request, auth: AuthContext, domain_id: str
) -> ResourceAccess:
    db = _pool(request)
    try:
        domain_uuid = uuid.UUID(domain_id)
    except ValueError as exc:
        raise not_found("domain") from exc
    row = await db.pool.fetchrow(
        "SELECT * FROM auth_domain_access($1, $2)", auth.user_id, domain_uuid
    )
    if row is None:
        raise not_found("domain")
    return ResourceAccess(
        org_id=str(row["org_id"]),
        role=row["role"],
        resource_id=str(row["domain_id"]),
        project_id=str(row["project_id"]),
    )


async def resolve_incident(
    request: Request, auth: AuthContext, incident_id: str
) -> ResourceAccess:
    db = _pool(request)
    try:
        incident_uuid = uuid.UUID(incident_id)
    except ValueError as exc:
        raise not_found("incident") from exc
    row = await db.pool.fetchrow(
        "SELECT * FROM auth_incident_access($1, $2)",
        auth.user_id,
        incident_uuid,
    )
    if row is None:
        raise not_found("incident")
    return ResourceAccess(
        org_id=str(row["org_id"]),
        role=row["role"],
        resource_id=str(row["incident_id"]),
        project_id=str(row["project_id"]),
    )


def require_role(access: OrgAccess | ResourceAccess, allowed: Iterable[Role]) -> None:
    if access.role not in set(allowed):
        raise forbidden()


def require_admin(access: OrgAccess | ResourceAccess) -> None:
    require_role(access, ADMIN_ROLES)


async def create_session(
    request: Request,
    *,
    user_id: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[str, str]:
    """Insert a session and return `(plaintext_token, session_id)`."""
    settings: Settings = request.app.state.settings
    db = _pool(request)
    token = new_token()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=max(settings.session_ttl_hours, 1))
    idle_expires_at = now + timedelta(hours=max(settings.session_idle_hours, 1))
    async with db_module.user_scope(db, user_id) as conn:
        session_id = await conn.fetchval(
            """
            INSERT INTO sessions
                (user_id, token_hash, expires_at, idle_expires_at, ip, user_agent)
            VALUES ($1, $2, $3, $4, $5::inet, $6)
            RETURNING id
            """,
            uuid.UUID(user_id),
            hash_token(token),
            expires_at,
            idle_expires_at,
            ip,
            user_agent[:512] if user_agent else None,
        )
    return token, str(session_id)


def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max(settings.session_ttl_hours, 1) * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


async def revoke_session(request: Request, auth: AuthContext) -> None:
    db = _pool(request)
    async with db_module.user_scope(db, auth.user_id) as conn:
        await conn.execute(
            """
            UPDATE sessions
               SET revoked_at = now(), revoke_reason = 'user_logout'
             WHERE id = $1 AND revoked_at IS NULL
            """,
            uuid.UUID(auth.session_id),
        )


__all__ = [
    "ADMIN_ROLES",
    "ALL_ROLES",
    "AuthContext",
    "OrgAccess",
    "ResourceAccess",
    "clear_session_cookie",
    "create_session",
    "get_auth",
    "hash_token",
    "list_orgs",
    "new_token",
    "require_admin",
    "require_role",
    "resolve_deployment",
    "resolve_domain",
    "resolve_incident",
    "resolve_org",
    "resolve_project",
    "revoke_session",
    "set_session_cookie",
]
