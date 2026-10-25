"""Secret metadata and writes (SPEC-18, SPEC-36 §3.4).

The control plane never resolves a secret value. Writes go through the same
envelope service the build/runtime workers use, so the AAD binding, audit-before-
write ordering and confirmation rules cannot diverge between paths. The API
returns metadata only; a value is never echoed.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from control import db as db_module
from control.errors import ApiProblem, not_found
from control.models import SecretOut, SecretPutOut, SecretUpsertIn
from control.security import AuthContext, get_auth, require_admin, resolve_project
from control.settings import Settings, get_settings
from runtime.secrets.audit import AsyncpgAuditSink
from runtime.secrets.errors import (
    AuditWriteFailed,
    ConfirmationRequired,
    InvalidSecret,
    MasterKeyUnavailable,
    SecretAlreadyExists,
    SecretNotFound,
    SecretQuotaExceeded,
)
from runtime.secrets.masterkey import FileMasterKey, MasterKeyProvider
from runtime.secrets.service import SecretMetadata, SecretService

router = APIRouter(tags=["secrets"])
Auth = Annotated[AuthContext, Depends(get_auth)]


@lru_cache(maxsize=4)
def _master_key(path: str) -> MasterKeyProvider:
    return FileMasterKey(path)


def _service(request: Request, settings: Settings) -> SecretService:
    db: db_module.Db = request.app.state.db
    if db.audit_pool is None:
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "the audit database is not configured; secrets are unavailable",
        )
    if not settings.master_key_file:
        raise ApiProblem(
            503,
            "upstream_unavailable",
            "MASTER_KEY_FILE is not configured; secrets are unavailable",
        )
    try:
        master = _master_key(settings.master_key_file)
    except MasterKeyUnavailable as exc:
        raise ApiProblem(
            503, "upstream_unavailable", "the secret master key is unavailable"
        ) from exc
    return SecretService(master_key=master, audit=AsyncpgAuditSink(db.audit_pool))


def _out(meta: SecretMetadata) -> SecretOut:
    return SecretOut(
        key=meta.key,
        target=meta.target,
        environments=list(meta.environments),
        last_used_at=meta.last_used_at,
    )


def _translate(exc: Exception) -> ApiProblem:
    if isinstance(exc, SecretQuotaExceeded):
        return ApiProblem(409, "quota_exceeded", "the project secret limit is reached")
    if isinstance(exc, SecretAlreadyExists):
        return ApiProblem(409, "conflict", "a secret with that key already exists")
    if isinstance(exc, SecretNotFound):
        return not_found("secret")
    if isinstance(exc, ConfirmationRequired):
        return ApiProblem(
            422,
            "validation_failed",
            "this visibility needs an explicit confirmation",
            fields={"confirmation": str(exc)},
        )
    if isinstance(exc, InvalidSecret):
        return ApiProblem(422, "validation_failed", str(exc))
    if isinstance(exc, (MasterKeyUnavailable, AuditWriteFailed)):
        return ApiProblem(
            503,
            "upstream_unavailable",
            "the secret store is unavailable; the write was not applied",
            headers={"Retry-After": "5"},
        )
    return ApiProblem(500, "internal_error", "the secret operation failed")


@router.get("/projects/{project_id}/secrets", response_model=list[SecretOut])
async def list_secrets(
    request: Request,
    project_id: str,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[SecretOut]:
    access = await resolve_project(request, auth, project_id)
    db: db_module.Db = request.app.state.db
    service = _service(request, settings)
    async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
        rows = await service.list_secrets(
            conn, org_id=access.org_id, project_id=project_id
        )
    return [_out(row) for row in rows]


@router.put("/projects/{project_id}/secrets/{key}", response_model=SecretPutOut)
async def put_secret(
    request: Request,
    project_id: str,
    key: str,
    body: SecretUpsertIn,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SecretPutOut:
    access = await resolve_project(request, auth, project_id)
    require_admin(access)
    if not body.value:
        raise ApiProblem(
            422,
            "validation_failed",
            "value must not be empty",
            fields={"value": "required"},
        )
    confirm_build = body.confirm_build_visibility or body.confirmation
    confirm_non_production = (
        body.confirm_non_production_environments or body.confirmation
    )
    db: db_module.Db = request.app.state.db
    service = _service(request, settings)
    try:
        async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
            existing = await conn.fetchval(
                "SELECT 1 FROM env_vars WHERE org_id = $1 AND project_id = $2 AND key = $3",
                uuid.UUID(access.org_id),
                uuid.UUID(project_id),
                key,
            )
            if existing:
                meta = await service.update_secret(
                    conn,
                    org_id=access.org_id,
                    project_id=project_id,
                    key=key,
                    value=body.value,
                    target=body.target,
                    environments=body.environments,
                    confirm_build_visibility=confirm_build,
                    confirm_non_production_environments=confirm_non_production,
                    actor_id=auth.user_id,
                )
            else:
                meta = await service.create_secret(
                    conn,
                    org_id=access.org_id,
                    project_id=project_id,
                    key=key,
                    value=body.value,
                    target=body.target,
                    environments=body.environments,
                    confirm_build_visibility=confirm_build,
                    confirm_non_production_environments=confirm_non_production,
                    actor_id=auth.user_id,
                )
    except (SecretQuotaExceeded, SecretAlreadyExists, SecretNotFound,
            ConfirmationRequired, InvalidSecret, MasterKeyUnavailable,
            AuditWriteFailed) as exc:
        raise _translate(exc) from exc
    return SecretPutOut(
        key=meta.key,
        value=body.value,
        target=meta.target,
        environments=list(meta.environments),
    )


@router.delete("/projects/{project_id}/secrets/{key}", status_code=204)
async def delete_secret(
    request: Request,
    project_id: str,
    key: str,
    auth: Auth,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    access = await resolve_project(request, auth, project_id)
    require_admin(access)
    db: db_module.Db = request.app.state.db
    service = _service(request, settings)
    try:
        async with db_module.tenant_scope(db, access.org_id, auth.user_id) as conn:
            await service.delete_secret(
                conn,
                org_id=access.org_id,
                project_id=project_id,
                key=key,
                actor_id=auth.user_id,
            )
    except (SecretNotFound, MasterKeyUnavailable, AuditWriteFailed, InvalidSecret) as exc:
        raise _translate(exc) from exc
