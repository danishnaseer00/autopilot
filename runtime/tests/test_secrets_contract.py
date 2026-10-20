"""SPEC-18 acceptance criteria against a live Postgres + audit database.

Requires the compose stack from ``paas/docker-compose.dev.yml`` and:
  DATABASE_URL        migrator DSN (schema applied)
  APP_DATABASE_URL    autopilot_app DSN (RLS subject)
  AUDIT_DATABASE_URL  autopilot_audit DSN (AC-10's persisted-row check)

Skips cleanly when the DSNs are absent, exactly like the SPEC-03 contract suite.
The tests provision their own org/project rows as the migrator and clean up the
org afterwards; audit rows are append-only by design and are left behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import uuid
from typing import Sequence

import asyncpg
import pytest

from runtime.secrets.audit import (
    AsyncpgAuditSink,
    InMemoryAuditSink,
)
from runtime.secrets.errors import (
    AuditWriteFailed,
    ConfirmationRequired,
    MasterKeyUnavailable,
    SecretDecryptDenied,
    SecretNotFound,
    SecretQuotaExceeded,
)
from runtime.secrets.masterkey import StaticMasterKey, UnavailableMasterKey
from runtime.secrets.service import SecretService

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") and os.environ.get("APP_DATABASE_URL")),
    reason="DATABASE_URL / APP_DATABASE_URL not configured",
)

MIGRATOR_DSN = os.environ.get("DATABASE_URL", "")
APP_DSN = os.environ.get("APP_DATABASE_URL", "")
AUDIT_DSN = os.environ.get("AUDIT_DATABASE_URL", "")


# ===== fixtures =============================================================


@pytest.fixture
def tenants():
    state = asyncio.run(_provision())
    try:
        yield state
    finally:
        asyncio.run(_cleanup(state))


async def _provision() -> dict:
    tag = uuid.uuid4().hex[:12]
    conn = await asyncpg.connect(MIGRATOR_DSN)
    try:
        org = await conn.fetchval(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING id",
            f"Secrets {tag}",
            f"secrets-{tag}",
        )
        project = await conn.fetchval(
            "INSERT INTO projects "
            "  (org_id, name, slug, provider, provider_repo_id, k8s_namespace) "
            "VALUES ($1, $2, $3, 'github', $4, $5) RETURNING id",
            org,
            "Storefront",
            f"storefront-{tag}",
            int(tag, 16) % 2_000_000_000,
            f"autopilot-{tag}",
        )
        other_org = await conn.fetchval(
            "INSERT INTO orgs (name, slug) VALUES ($1, $2) RETURNING id",
            f"Other {tag}",
            f"other-{tag}",
        )
        other_project = await conn.fetchval(
            "INSERT INTO projects "
            "  (org_id, name, slug, provider, provider_repo_id, k8s_namespace) "
            "VALUES ($1, $2, $3, 'github', $4, $5) RETURNING id",
            other_org,
            "Other",
            f"other-project-{tag}",
            int(tag, 16) % 2_000_000_000 + 1,
            f"autopilot-other-{tag}",
        )
        return {
            "org_id": org,
            "project_id": project,
            "other_org_id": other_org,
            "other_project_id": other_project,
        }
    finally:
        await conn.close()


async def _cleanup(state: dict) -> None:
    conn = await asyncpg.connect(MIGRATOR_DSN)
    try:
        await conn.execute(
            "DELETE FROM orgs WHERE id = ANY($1::uuid[])",
            [state["org_id"], state["other_org_id"]],
        )
    finally:
        await conn.close()


@contextlib.asynccontextmanager
async def _app_tx(org_id: uuid.UUID, *, commit: bool = True):
    """An application-role transaction with transaction-local tenant context."""
    conn = await asyncpg.connect(APP_DSN)
    tx = conn.transaction()
    await tx.start()
    await conn.execute("SELECT set_config('app.org_id', $1, true)", str(org_id))
    try:
        yield conn
    except BaseException:
        await tx.rollback()
        raise
    else:
        await tx.commit() if commit else await tx.rollback()
    finally:
        await conn.close()


def _service(master=None, audit=None, max_secrets: int = 50) -> SecretService:
    return SecretService(
        master_key=master or StaticMasterKey(os.urandom(32)),
        audit=audit or InMemoryAuditSink(),
        max_secrets_per_project=max_secrets,
    )


async def _create(
    service: SecretService,
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    key: str,
    value: str,
    target: str = "runtime",
    environments: Sequence[str] = ("production",),
    confirm_build: bool | None = None,
    confirm_envs: bool | None = None,
):
    if confirm_build is None:
        confirm_build = target in ("build", "both")
    if confirm_envs is None:
        confirm_envs = any(environment != "production" for environment in environments)
    return await service.create_secret(
        conn,
        org_id=org_id,
        project_id=project_id,
        key=key,
        value=value,
        target=target,
        environments=environments,
        confirm_build_visibility=confirm_build,
        confirm_non_production_environments=confirm_envs,
        actor_id="user-1",
    )


async def _deployment(
    conn: asyncpg.Connection,
    *,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    status: str,
    environment: str = "production",
    build_env_keys: Sequence[str] = (),
    revision: str | None = None,
) -> uuid.UUID:
    return await conn.fetchval(
        "INSERT INTO deployments "
        "  (project_id, org_id, git_sha, git_ref, environment, trigger_source, "
        "   status, manifest, manifest_hash, build_image, build_env_keys, revision) "
        "VALUES ($1, $2, $3, 'refs/heads/main', $4, 'api', $5, '{}'::jsonb, "
        "        $6, $7, $8, $9) RETURNING id",
        project_id,
        org_id,
        uuid.uuid4().hex,
        environment,
        status,
        uuid.uuid4().hex,
        "registry.local/storefront@sha256:" + "a" * 64,
        list(build_env_keys),
        revision or uuid.uuid4().hex[:12],
    )


# ===== AC-1: a row's ciphertext cannot be moved to another row =============


def test_ac1_row_swap_fails_to_decrypt(tenants) -> None:
    async def scenario() -> None:
        audit = InMemoryAuditSink()
        service = _service(audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="A_KEY", value="row-a-value",
            )
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="B_KEY", value="row-b-value",
            )
            row_a = await conn.fetchrow(
                "SELECT ciphertext, data_key FROM env_vars WHERE key = 'A_KEY'"
            )
            # Copy both the ciphertext *and* the wrapped DEK into row B: even
            # with the key material, the AAD names row B, so authentication
            # fails instead of returning row A's value under B's name.
            await conn.execute(
                "UPDATE env_vars SET ciphertext = $1, data_key = $2 WHERE key = 'B_KEY'",
                row_a["ciphertext"],
                row_a["data_key"],
            )
            with pytest.raises(SecretDecryptDenied):
                await service.resolve_for_runtime(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    environment="production",
                )
        assert "secret.decrypt_denied" in audit.actions()

    asyncio.run(scenario())


# ===== AC-2: a fork build receives {} while a normal build receives values ===


def test_ac2_fork_build_gets_nothing(tenants) -> None:
    async def scenario() -> None:
        audit = InMemoryAuditSink()
        service = _service(audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="BUILD_TOKEN", value="build-token-value", target="build",
            )
            assert await service.resolve_for_build(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
                fork=True,
            ) == {}
            # The fork path wrote no read audit row: it never looked anything up.
            assert "secret.read_value" not in audit.actions()

            values = await service.resolve_for_build(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
                build_id=uuid.uuid4(),
            )
            assert values == {"BUILD_TOKEN": "build-token-value"}

    asyncio.run(scenario())


# ===== AC-3 / R-6: production secrets never reach previews ==================


def test_ac3_production_value_never_reaches_preview(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="DATABASE_URL", value="postgres://prod-only/v1",
            )
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
            ) == {"DATABASE_URL": "postgres://prod-only/v1"}
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="preview",
            ) == {}

            await service.update_secret(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                key="DATABASE_URL",
                value="postgres://prod-only/v2",
                actor_id="user-1",
            )
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="preview",
            ) == {}
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
            ) == {"DATABASE_URL": "postgres://prod-only/v2"}

    asyncio.run(scenario())


# ===== AC-7: build visibility is opt-in and audited =========================


def test_ac7_build_target_requires_confirmation_and_writes_one_audit_row(tenants) -> None:
    async def scenario() -> None:
        audit = InMemoryAuditSink()
        service = _service(audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            with pytest.raises(ConfirmationRequired):
                await _create(
                    service, conn, org_id=tenants["org_id"],
                    project_id=tenants["project_id"], key="BUILD_TOKEN",
                    value="must-not-be-stored", target="build", confirm_build=False,
                )
            assert await conn.fetchval(
                "SELECT count(*) FROM env_vars WHERE key = 'BUILD_TOKEN'"
            ) == 0

            await _create(
                service, conn, org_id=tenants["org_id"],
                project_id=tenants["project_id"], key="BUILD_TOKEN",
                value="must-not-be-stored", target="build",
            )
            assert audit.actions() == ["secret.write"]
            assert audit.rows[0].metadata["build_visible"] is True
            assert "must-not-be-stored" not in str(audit.rows[0].metadata)

            # preview visibility has the same explicit gate (§8)
            with pytest.raises(ConfirmationRequired):
                await _create(
                    service, conn, org_id=tenants["org_id"],
                    project_id=tenants["project_id"], key="PREVIEW_TOKEN",
                    value="must-not-be-stored", environments=["preview"],
                    confirm_envs=False,
                )

    asyncio.run(scenario())


# ===== AC-8: rotation is atomic across deployments ==========================


def test_ac8_rotation_updates_every_live_deployment_in_one_transaction(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="DB_PASSWORD", value="first-value",
            )
            prod_live = await _deployment(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                status="live", environment="production",
            )
            preview_live = await _deployment(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                status="live", environment="preview",
            )
            preview_ready = await _deployment(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                status="ready", environment="preview",
            )

            result = await service.rotate_secret(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                key="DB_PASSWORD",
                value="second-value",
                actor_id="user-1",
            )

            assert result.version == 1
            assert result.live_deployments == 2
            assert result.stale_previews == 1
            assert await conn.fetchval(
                "SELECT secrets_version FROM projects WHERE id = $1",
                tenants["project_id"],
            ) == 1
            versions = await conn.fetch(
                "SELECT id, secrets_version, stale_secrets FROM deployments "
                "WHERE id = ANY($1::uuid[])",
                [prod_live, preview_live, preview_ready],
            )
            by_id = {row["id"]: row for row in versions}
            assert by_id[prod_live]["secrets_version"] == 1
            assert by_id[preview_live]["secrets_version"] == 1
            assert by_id[preview_ready]["stale_secrets"] is True
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
            ) == {"DB_PASSWORD": "second-value"}

        # A failed/rolled-back rotation leaves both the ciphertext and the
        # version untouched: the transaction is the unit of atomicity.
        async with _app_tx(tenants["org_id"], commit=False) as conn:
            await service.rotate_secret(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                key="DB_PASSWORD",
                value="rolled-back-value",
                actor_id="user-1",
            )
        async with _app_tx(tenants["org_id"]) as conn:
            assert await conn.fetchval(
                "SELECT secrets_version FROM projects WHERE id = $1",
                tenants["project_id"],
            ) == 1
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
            ) == {"DB_PASSWORD": "second-value"}

    asyncio.run(scenario())


# ===== AC-9 / R-12: deletion is real and marks referencing deployments ======


def test_ac9_delete_marks_referencing_deployments_stale(tenants) -> None:
    async def scenario() -> None:
        audit = InMemoryAuditSink()
        service = _service(audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            created = await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="BUILD_TOKEN", value="delete-me-value",
            )
            referencing = await _deployment(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                status="ready", environment="production",
                build_env_keys=["BUILD_TOKEN"],
            )
            unrelated = await _deployment(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                status="ready", environment="staging",
            )

            marked = await service.delete_secret(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                key="BUILD_TOKEN",
                actor_id="user-1",
            )

            assert marked == 1
            assert await conn.fetchval(
                "SELECT count(*) FROM env_vars WHERE id = $1", created.id
            ) == 0
            rows = await conn.fetch(
                "SELECT id, stale_secrets FROM deployments WHERE id = ANY($1::uuid[])",
                [referencing, unrelated],
            )
            by_id = {row["id"]: row["stale_secrets"] for row in rows}
            assert by_id[referencing] is True
            assert by_id[unrelated] is False
            assert "secret.delete" in audit.actions()

            with pytest.raises(SecretNotFound):
                await service.delete_secret(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    key="BUILD_TOKEN",
                    actor_id="user-1",
                )

    asyncio.run(scenario())


# ===== AC-10: every resolution is audited; audit failure blocks the value ===


def test_ac10_every_resolution_is_audited_and_audit_failure_fails_closed(
    tenants,
) -> None:
    async def scenario() -> None:
        master = StaticMasterKey(os.urandom(32))
        audit = InMemoryAuditSink()
        service = _service(master=master, audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            created = await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="API_KEY", value="audited-value",
            )
            assert audit.actions() == ["secret.write"]
            audit.rows.clear()

            values = await service.resolve_for_runtime(
                conn,
                org_id=tenants["org_id"],
                project_id=tenants["project_id"],
                environment="production",
                deployment_id=uuid.uuid4(),
            )
            assert values == {"API_KEY": "audited-value"}
            assert audit.actions() == ["secret.read_value"]
            assert "audited-value" not in str(audit.rows[0].metadata)

            failing = _service(master=master, audit=InMemoryAuditSink(fail=True))
            with pytest.raises(AuditWriteFailed):
                await failing.resolve_for_runtime(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    environment="production",
                )

        if not AUDIT_DSN:
            return
        # The real sink writes to autopilot_audit. Verify the row is persisted
        # before the value is observable, and that metadata holds no value.
        pool = await asyncpg.create_pool(AUDIT_DSN, min_size=0, max_size=2)
        try:
            real_service = _service(master=master, audit=AsyncpgAuditSink(pool))
            async with _app_tx(tenants["org_id"]) as conn:
                await real_service.resolve_for_runtime(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    environment="production",
                )
        finally:
            await pool.close()

        audit_conn = await asyncpg.connect(AUDIT_DSN)
        try:
            row = await audit_conn.fetchrow(
                "SELECT metadata FROM audit_log "
                "WHERE action = 'secret.read_value' AND target_id = $1 "
                "ORDER BY id DESC LIMIT 1",
                str(created.id),
            )
        finally:
            await audit_conn.close()
        assert row is not None
        assert "audited-value" not in str(row["metadata"])

    asyncio.run(scenario())


# ===== AC-11: an unavailable key provider fails closed ======================


def test_ac11_unavailable_master_key_fails_builds_closed(tenants) -> None:
    async def scenario() -> None:
        working = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                working, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="BUILD_TOKEN", value="needs-a-key", target="build",
            )
        degraded = _service(master=UnavailableMasterKey("KMS unavailable"))
        async with _app_tx(tenants["org_id"]) as conn:
            with pytest.raises(MasterKeyUnavailable):
                await degraded.resolve_for_build(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    environment="production",
                )
            # A write must fail too: never store a secret whose DEK cannot be
            # unwrapped and whose decrypts would later fail.
            with pytest.raises(MasterKeyUnavailable):
                await degraded.create_secret(
                    conn,
                    org_id=tenants["org_id"],
                    project_id=tenants["project_id"],
                    key="NEW_SECRET",
                    value="must-not-be-stored",
                    actor_id="user-1",
                )
            assert await conn.fetchval(
                "SELECT count(*) FROM env_vars WHERE key = 'NEW_SECRET'"
            ) == 0

    asyncio.run(scenario())


def test_ac4_plaintext_is_not_persisted(tenants) -> None:
    """AC-4's storage/audit half; the pod-spec half lands with SPEC-14.

    No plaintext in the env_vars row, the opaque metadata, or audit metadata.
    The build image layer is SPEC-09's check; rendering a pod spec that uses a
    Secret reference is SPEC-14's.
    """

    async def scenario() -> None:
        value = "plaintext-marker-do-not-persist"
        audit = InMemoryAuditSink()
        service = _service(audit=audit)
        async with _app_tx(tenants["org_id"]) as conn:
            created = await _create(
                service, conn, org_id=tenants["org_id"],
                project_id=tenants["project_id"], key="API_KEY", value=value,
            )
            row = await conn.fetchrow(
                "SELECT ciphertext, data_key FROM env_vars WHERE id = $1",
                created.id,
            )
            assert value.encode() not in bytes(row["ciphertext"])
            assert value.encode() not in bytes(row["data_key"])

            listed = await service.list_secrets(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"]
            )
            assert value not in repr(listed)
            assert value not in repr(audit.rows)

    asyncio.run(scenario())


# ===== other contract properties ===========================================


def test_r4_no_decryption_cache_after_rotation(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="API_KEY", value="value-one",
            )
            assert (await service.resolve_for_runtime(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                environment="production",
            ))["API_KEY"] == "value-one"

            await service.rotate_secret(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="API_KEY", value="value-two", actor_id="user-1",
            )
            # Same service instance, same connection: a cache would return v1.
            assert (await service.resolve_for_runtime(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                environment="production",
            ))["API_KEY"] == "value-two"

    asyncio.run(scenario())


def test_per_org_dek_is_reused_across_secrets(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            for key in ("FIRST_KEY", "SECOND_KEY"):
                await _create(
                    service, conn, org_id=tenants["org_id"],
                    project_id=tenants["project_id"], key=key, value="value-long-enough",
                )
            assert await conn.fetchval(
                "SELECT count(DISTINCT data_key) FROM env_vars WHERE project_id = $1",
                tenants["project_id"],
            ) == 1
            assert await conn.fetchval(
                "SELECT dek_wrapped IS NOT NULL FROM orgs WHERE id = $1",
                tenants["org_id"],
            ) is True

    asyncio.run(scenario())


def test_cross_tenant_resolution_returns_nothing(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="API_KEY", value="tenant-a-value",
            )
        # Org B's context + org A's project id: the explicit org predicate plus
        # RLS return zero rows, never A's value.
        async with _app_tx(tenants["other_org_id"]) as conn:
            assert await service.resolve_for_runtime(
                conn,
                org_id=tenants["other_org_id"],
                project_id=tenants["project_id"],
                environment="production",
            ) == {}
            assert await service.list_secrets(
                conn,
                org_id=tenants["other_org_id"],
                project_id=tenants["project_id"],
            ) == []

    asyncio.run(scenario())


def test_list_returns_metadata_only(tenants) -> None:
    async def scenario() -> None:
        service = _service()
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="API_KEY", value="never-in-metadata",
            )
            listed = await service.list_secrets(
                conn, org_id=tenants["org_id"], project_id=tenants["project_id"]
            )
            assert [secret.key for secret in listed] == ["API_KEY"]
            assert listed[0].target == "runtime"
            assert listed[0].environments == ("production",)
            assert "never-in-metadata" not in str(dataclasses.asdict(listed[0]))

    asyncio.run(scenario())


def test_quota_rejects_before_storing(tenants) -> None:
    async def scenario() -> None:
        service = _service(max_secrets=1)
        async with _app_tx(tenants["org_id"]) as conn:
            await _create(
                service, conn, org_id=tenants["org_id"], project_id=tenants["project_id"],
                key="ONE", value="first-value",
            )
            with pytest.raises(SecretQuotaExceeded):
                await _create(
                    service, conn, org_id=tenants["org_id"],
                    project_id=tenants["project_id"], key="TWO",
                    value="second-value",
                )
            assert await conn.fetchval(
                "SELECT count(*) FROM env_vars WHERE project_id = $1",
                tenants["project_id"],
            ) == 1

    asyncio.run(scenario())
