"""Database access for the control plane.

Three rules from SPEC-03 that this module makes hard to violate:

1. RLS is the backstop, not the mechanism (R-4): every tenant query goes
   through `tenant_scope`, which opens an explicit transaction and sets
   transaction-local context. Transaction-local (set_config's third argument
   `true`) means the context dies with the transaction, so a connection that
   returns to the pool cannot leak org A's identity into org B's request
   (AC-5).
2. The app role has no BYPASSRLS (R-2) and the app never loads one: there is
   no migrator DSN anywhere in this module.
3. Audit rows are written to the separate audit database through the audit
   role's own pool (SPEC-03 §3) using `write_audit` - call it BEFORE the
   audited operation executes (SPEC-02 R-9), never after.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

import asyncpg

from control.settings import Settings

_TENANT_TABLES_HINT = "queries must also carry explicit org_id predicates (SPEC-03 R-4)"


async def _init_connection(conn: asyncpg.Connection) -> None:
    """Decode json/jsonb to Python objects, not strings.

    asyncpg leaves JSONB as text by default. The API models and the secret/
    deployment services all expect mappings; doing this at the pool boundary
    avoids a per-call `json.loads` that one new route would forget.
    """
    for type_name in ("json", "jsonb"):
        await conn.set_type_codec(
            type_name,
            encoder=json.dumps,
            decoder=json.loads,
            schema="pg_catalog",
        )


class Db:
    """Owns both pools. Created via create_app's lifespan."""

    def __init__(self) -> None:
        self.pool: asyncpg.Pool | None = None
        self.audit_pool: asyncpg.Pool | None = None

    async def connect(self, settings: Settings) -> None:
        if settings.app_database_url and self.pool is None:
            self.pool = await asyncpg.create_pool(
                settings.app_database_url,
                min_size=1,
                max_size=5,
                init=_init_connection,
            )
        if settings.audit_database_url and self.audit_pool is None:
            self.audit_pool = await asyncpg.create_pool(
                settings.audit_database_url,
                min_size=0,
                max_size=2,
                init=_init_connection,
            )

    async def close(self) -> None:
        for pool in (self.pool, self.audit_pool):
            if pool is not None:
                await pool.close()


@asynccontextmanager
async def user_scope(db: Db, user_id: str | uuid.UUID):
    """Transaction-local user context for the sessions table.

    A user is a platform identity, not a tenant. Session creation, rotation and
    logout happen before (or without) an org, so they use this narrower scope.
    """
    if db.pool is None:
        raise RuntimeError("database pool is not configured (set app_database_url)")
    conn = await db.pool.acquire()
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.user_id', $1, true)", str(user_id))
        try:
            yield conn
        except BaseException:
            await tx.rollback()
            raise
        else:
            await tx.commit()
    finally:
        await db.pool.release(conn)


@asynccontextmanager
async def tenant_scope(db: Db, org_id: str | uuid.UUID, user_id: str | uuid.UUID | None = None):
    """Check out a connection with transaction-local tenant context set.

    Usage:
        async with tenant_scope(db, org_id, user_id) as conn:
            rows = await conn.fetch("SELECT ... WHERE org_id = $1", org_id)

    Includes the org_id predicate anyway - RLS silently turning a forgotten
    filter into an empty result is safe but hides the bug (R-4).
    """
    if db.pool is None:
        raise RuntimeError("database pool is not configured (set app_database_url)")
    org = str(org_id)
    user = str(user_id) if user_id is not None else None

    conn = await db.pool.acquire()
    try:
        tx = conn.transaction()
        await tx.start()
        await conn.execute("SELECT set_config('app.org_id', $1, true)", org)
        if user is not None:
            await conn.execute("SELECT set_config('app.user_id', $1, true)", user)
        try:
            yield conn
        except BaseException:
            await tx.rollback()
            raise
        else:
            await tx.commit()
    finally:
        await db.pool.release(conn)


async def write_audit(
    db: Db,
    *,
    action: str,
    actor_type: str,
    actor_id: str,
    org_id: str | uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Insert one audit row into SPEC-03 §4.10's audit database.

    Call BEFORE the audited operation executes (SPEC-02 R-9): the row must
    exist even if the operation itself fails, so the attempt is on record.
    """
    if db.audit_pool is None:
        raise RuntimeError("audit pool is not configured (set audit_database_url)")
    org = uuid.UUID(str(org_id)) if org_id is not None else None
    await db.audit_pool.execute(
        "INSERT INTO audit_log "
        "  (org_id, actor_type, actor_id, action, target_type, target_id, metadata) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)",
        org,
        actor_type,
        actor_id,
        action,
        target_type,
        target_id,
        json.dumps(metadata or {}),
    )


__all__ = ["Db", "tenant_scope", "user_scope", "write_audit", _TENANT_TABLES_HINT]
