"""Custom domain intent and DNS verification (SPEC-15, SPEC-36 §3.5)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends, Request

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import DnsRecordOut, DomainCreateIn, DomainOut
from control.security import (
    AuthContext,
    get_auth,
    require_admin,
    resolve_domain,
    resolve_project,
)

router = APIRouter(tags=["domains"])
Auth = Annotated[AuthContext, Depends(get_auth)]


def _out(row) -> DomainOut:
    return DomainOut(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        hostname=row["hostname"],
        status=row["status"],
        is_production=row["is_production"],
        verification_token=row["verification_token"],
        cert_issued_at=row["cert_issued_at"],
        cert_expires_at=row["cert_expires_at"],
        dns_instructions=[
            DnsRecordOut(
                type="TXT",
                name=f"_autopilot.{row['hostname']}",
                value=row["verification_token"],
            )
        ],
    )


@router.get("/projects/{project_id}/domains", response_model=list[DomainOut])
async def list_domains(
    request: Request, project_id: str, auth: Auth
) -> list[DomainOut]:
    access = await resolve_project(request, auth, project_id)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await conn.fetch(
            "SELECT * FROM domains WHERE org_id = $1 AND project_id = $2 "
            "ORDER BY created_at DESC",
            uuid.UUID(access.org_id),
            uuid.UUID(project_id),
        )
    return [_out(row) for row in rows]


@router.post(
    "/projects/{project_id}/domains", response_model=DomainOut, status_code=201
)
async def add_domain(
    request: Request,
    project_id: str,
    body: DomainCreateIn,
    auth: Auth,
) -> DomainOut:
    access = await resolve_project(request, auth, project_id)
    require_admin(access)
    hostname = body.hostname.strip().lower().rstrip(".")
    if (
        not hostname
        or "://" in hostname
        or "/" in hostname
        or " " in hostname
        or len(hostname) > 253
        or "." not in hostname
    ):
        raise ApiProblem(
            422,
            "validation_failed",
            "hostname must be a bare DNS name",
            fields={"hostname": "invalid hostname"},
        )
    db: db_module.Db = request.app.state.db
    token = f"autopilot-verify-{secrets.token_urlsafe(24)}"
    try:
        async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO domains
                    (org_id, project_id, hostname, is_production, verification_token)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
                """,
                uuid.UUID(access.org_id),
                uuid.UUID(project_id),
                hostname,
                body.is_production,
                token,
            )
    except asyncpg.UniqueViolationError as exc:
        raise ApiProblem(
            409, "conflict", "that hostname is already attached to a project"
        ) from exc
    return _out(row)


async def _txt_values(hostname: str) -> list[str]:
    try:
        import dns.resolver  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "DNS verification is not installed in this environment",
        ) from exc

    def resolve() -> list[str]:
        answers = dns.resolver.resolve(
            f"_autopilot.{hostname}", "TXT", lifetime=5
        )
        values: list[str] = []
        for answer in answers:
            text = b"".join(answer.strings).decode("utf-8", "replace")
            values.append(text)
        return values

    try:
        return await asyncio.to_thread(resolve)
    except Exception as exc:  # noqa: BLE001 - NXDOMAIN/timeout are normal outcomes
        return []


@router.post("/domains/{domain_id}/verify", response_model=DomainOut)
async def verify_domain(
    request: Request, domain_id: str, auth: Auth
) -> DomainOut:
    access = await resolve_domain(request, auth, domain_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        row = await conn.fetchrow(
            "SELECT * FROM domains WHERE id = $1 AND org_id = $2",
            uuid.UUID(domain_id),
            uuid.UUID(access.org_id),
        )
        if row is None:
            raise not_found("domain")
        if row["status"] == "active":
            return _out(row)
        values = await _txt_values(row["hostname"])
        if row["verification_token"] not in values:
            raise ApiProblem(
                409,
                "domain_unverified",
                "the verification TXT record was not found",
            )
        updated = await conn.fetchrow(
            "UPDATE domains SET status = 'verified' WHERE id = $1 RETURNING *",
            uuid.UUID(domain_id),
        )
    return _out(updated)


@router.delete("/domains/{domain_id}", status_code=204)
async def delete_domain(
    request: Request, domain_id: str, auth: Auth
) -> None:
    access = await resolve_domain(request, auth, domain_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        result = await conn.execute(
            "DELETE FROM domains WHERE id = $1 AND org_id = $2",
            uuid.UUID(domain_id),
            uuid.UUID(access.org_id),
        )
        if result != "DELETE 1":
            raise not_found("domain")
