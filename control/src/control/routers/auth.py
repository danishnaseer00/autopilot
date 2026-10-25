"""Session endpoints (SPEC-05, SPEC-36 §3.1)."""

from __future__ import annotations

import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request, Response

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import MeOut, OrgOut, UserOut
from control.security import (
    AuthContext,
    clear_session_cookie,
    create_session,
    get_auth,
    list_orgs,
    revoke_session,
    set_session_cookie,
)
from control.settings import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])
me_router = APIRouter(tags=["auth"])

Auth = Annotated[AuthContext, Depends(get_auth)]


def _me(auth: AuthContext, orgs) -> MeOut:
    return MeOut(
        user=UserOut(
            id=auth.user_id,
            email=auth.email,
            display_name=auth.display_name,
            is_platform_admin=auth.is_platform_admin,
        ),
        orgs=[
            OrgOut(
                id=org.org_id,
                slug=org.slug,
                name=org.name,
                role=org.role,
                status=org.status,
                plan_code=org.plan_code,
                plan_name=org.plan_name,
                plan_limits=org.plan_limits,
            )
            for org in orgs
        ],
        is_platform_admin=auth.is_platform_admin,
    )


@me_router.get("/me", response_model=MeOut)
async def me(request: Request, auth: Auth) -> MeOut:
    return _me(auth, await list_orgs(request, auth))


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    await revoke_session(request, auth)
    clear_session_cookie(response, settings)
    response.status_code = 204
    return response


@router.post("/dev-login", response_model=MeOut)
async def dev_login(
    request: Request,
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
) -> MeOut:
    """Create/enter a local-only developer workspace.

    This route is 404 unless `APP_ENV_NAME=dev` *and* `DEV_LOGIN_ENABLED=true`.
    Deployments set both to safe values, so the route is not part of the
    production surface. It exists because GitHub OAuth cannot be completed from
    an offline local cluster.
    """

    if settings.env_name != "dev" or not settings.dev_login_enabled:
        raise not_found("route")
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")

    email = settings.dev_login_email
    display_name = "Local Developer"
    user = await db.pool.fetchrow(
        """
        INSERT INTO users (email, display_name, last_login_at)
        VALUES ($1::citext, $2, now())
        ON CONFLICT (email) DO UPDATE
            SET last_login_at = now()
        RETURNING id, email::text AS email, display_name, is_platform_admin
        """,
        email,
        display_name,
    )
    user_id = str(user["id"])

    existing = await db.pool.fetch("SELECT * FROM auth_list_user_orgs($1)", user["id"])
    org = next((row for row in existing if row["slug"] == settings.dev_login_org), None)
    if org is None:
        org_id = uuid.uuid4()
        plan_id = await db.pool.fetchval(
            "SELECT id FROM plans ORDER BY is_default DESC, created_at LIMIT 1"
        )
        try:
            async with db_module.tenant_scope(db, org_id, user_id) as conn:
                await conn.execute(
                    """
                    INSERT INTO orgs (id, name, slug, plan_id)
                    VALUES ($1, $2, $3, $4)
                    """,
                    org_id,
                    "Local Development",
                    settings.dev_login_org,
                    plan_id,
                )
                await conn.execute(
                    """
                    INSERT INTO memberships (org_id, user_id, role)
                    VALUES ($1, $2, 'owner')
                    """,
                    org_id,
                    user["id"],
                )
        except asyncpg.UniqueViolationError as exc:
            raise ApiProblem(
                409,
                "conflict",
                "the local dev organization slug is already in use by another "
                "owner; set DEV_LOGIN_ORG to a different slug",
            ) from exc
        existing = await db.pool.fetch(
            "SELECT * FROM auth_list_user_orgs($1)", user["id"]
        )
        org = next(row for row in existing if row["slug"] == settings.dev_login_org)

    token, _ = await create_session(
        request,
        user_id=user_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    set_session_cookie(response, token, settings)
    auth = AuthContext(
        session_id="",
        user_id=user_id,
        email=user["email"],
        display_name=user["display_name"],
        is_platform_admin=bool(user["is_platform_admin"]),
    )
    return _me(auth, await list_orgs(request, auth))
