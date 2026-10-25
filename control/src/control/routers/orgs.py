"""Organization, plan and usage reads (SPEC-36 §3.1, §3.6)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request

from control import db as db_module
from control.errors import ApiProblem
from control.models import (
    ApiModel,
    OrgDetailOut,
    OrgOut,
    PlanOut,
    UsageItemOut,
)
from control.security import (
    AuthContext,
    OrgAccess,
    get_auth,
    resolve_org,
)
from control.settings import Settings, get_settings

router = APIRouter(tags=["orgs"])
Auth = Annotated[AuthContext, Depends(get_auth)]


def _org_out(org: OrgAccess) -> OrgOut:
    return OrgOut(
        id=org.org_id,
        slug=org.slug,
        name=org.name,
        role=org.role,
        status=org.status,
        plan_code=org.plan_code,
        plan_name=org.plan_name,
        plan_limits=org.plan_limits,
    )


def _limit(limits: dict, *names: str) -> int | float | None:
    for name in names:
        if name in limits and limits[name] is not None:
            try:
                return limits[name]
            except (TypeError, ValueError):
                return None
    return None


def _reset_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    if now.month == 12:
        return now.replace(
            year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return now.replace(
        month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


async def _usage(
    request: Request, org: OrgAccess
) -> list[UsageItemOut]:
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    async with db_module.tenant_scope(db, org.org_id) as conn:
        row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(builds_succeeded + builds_failed), 0) AS builds,
                   COALESCE(MAX(storage_bytes), 0) AS storage_bytes,
                   COALESCE(SUM(egress_bytes), 0) AS egress_bytes,
                   COALESCE(SUM(runtime_seconds), 0) AS runtime_seconds
              FROM usage_daily
             WHERE org_id = $1
               AND day >= date_trunc('month', now())::date
            """,
            uuid.UUID(org.org_id),
        )
        seats = await conn.fetchval(
            "SELECT count(*) FROM memberships WHERE org_id = $1",
            uuid.UUID(org.org_id),
        )
    limits = org.plan_limits or {}
    reset = _reset_at()
    builds_now = int(row["builds"] or 0)
    builds_limit = _limit(limits, "builds_per_month", "buildsPerMonth", "monthlyBuilds")
    storage_now = int(row["storage_bytes"] or 0)
    storage_gb = _limit(limits, "storage_gb", "storageGb")
    storage_limit = int(storage_gb * 1024**3) if storage_gb is not None else None
    seats_limit = _limit(limits, "seats", "members")
    egress_now = int(row["egress_bytes"] or 0)
    egress_limit = _limit(limits, "egress_gb", "egressGb")
    egress_limit = (
        int(egress_limit * 1024**3) if egress_limit is not None else None
    )

    def item(name: str, current, limit) -> UsageItemOut:
        percent = None
        if limit is not None and limit > 0:
            percent = round((float(current) / float(limit)) * 100, 2)
        return UsageItemOut(
            name=name,
            current=current,
            limit=limit,
            reset_at=reset,
            percent=percent,
        )

    return [
        item("builds", builds_now, builds_limit),
        item("storage_bytes", storage_now, storage_limit),
        item("seats", int(seats or 0), seats_limit),
        item("egress_bytes", egress_now, egress_limit),
    ]


async def _plan(request: Request, code: str | None) -> PlanOut | None:
    if not code:
        return None
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    row = await db.pool.fetchrow(
        "SELECT code, name, price_cents, limits, is_default FROM plans WHERE code = $1",
        code,
    )
    if row is None:
        return None
    return PlanOut(
        code=row["code"],
        name=row["name"],
        price_cents=row["price_cents"],
        limits=dict(row["limits"] or {}),
        is_default=row["is_default"],
    )


@router.get("/orgs/{slug}", response_model=OrgDetailOut)
async def org_detail(request: Request, slug: str, auth: Auth) -> OrgDetailOut:
    org = await resolve_org(request, auth, slug)
    return OrgDetailOut(
        org=_org_out(org),
        plan=await _plan(request, org.plan_code),
        usage=await _usage(request, org),
    )


@router.get("/orgs/{slug}/usage", response_model=list[UsageItemOut])
async def org_usage(request: Request, slug: str, auth: Auth) -> list[UsageItemOut]:
    org = await resolve_org(request, auth, slug)
    return await _usage(request, org)


@router.get("/plans", response_model=list[PlanOut])
async def plans(request: Request) -> list[PlanOut]:
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    rows = await db.pool.fetch(
        "SELECT code, name, price_cents, limits, is_default "
        "FROM plans ORDER BY price_cents, code"
    )
    return [
        PlanOut(
            code=row["code"],
            name=row["name"],
            price_cents=row["price_cents"],
            limits=dict(row["limits"] or {}),
            is_default=row["is_default"],
        )
        for row in rows
    ]


class OrgCreateIn(ApiModel):
    name: str
    slug: str


@router.post("/orgs", response_model=OrgOut, status_code=201)
async def create_org(
    request: Request,
    body: OrgCreateIn,
    auth: Auth,
) -> OrgOut:
    """First-user bootstrap: create an org and make the caller its owner."""

    name = body.name.strip()
    slug = body.slug.strip().lower()
    if not name or not slug:
        raise ApiProblem(
            422,
            "validation_failed",
            "name and slug are required",
            fields={
                key: "required"
                for key, value in (("name", name), ("slug", slug))
                if not value
            },
        )
    if not all(ch.isalnum() or ch in "-_." for ch in slug) or len(slug) > 64:
        raise ApiProblem(
            422,
            "validation_failed",
            "slug must be at most 64 letters, digits, '.', '_' or '-'",
            fields={"slug": "invalid slug"},
        )
    db: db_module.Db = request.app.state.db
    if db.pool is None:
        raise ApiProblem(503, "upstream_unavailable", "database is not configured")
    org_id = uuid.uuid4()
    plan_id = await db.pool.fetchval(
        "SELECT id FROM plans ORDER BY is_default DESC, price_cents LIMIT 1"
    )
    try:
        async with db_module.tenant_scope(db, org_id, auth.user_id) as conn:
            await conn.execute(
                "INSERT INTO orgs (id, name, slug, plan_id) VALUES ($1, $2, $3, $4)",
                org_id,
                name,
                slug,
                plan_id,
            )
            await conn.execute(
                "INSERT INTO memberships (org_id, user_id, role) "
                "VALUES ($1, $2, 'owner')",
                org_id,
                uuid.UUID(auth.user_id),
            )
    except asyncpg.UniqueViolationError as exc:
        raise ApiProblem(409, "conflict", "that organization slug is already in use") from exc

    # Read it back through the same access path the rest of the API uses.
    org = await resolve_org(request, auth, slug)
    return _org_out(org)
