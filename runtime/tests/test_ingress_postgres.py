"""Optional SPEC-15 Postgres adapter tests.

They run only when the local migrator DSN is present.  The CI contract suite
owns the real-device checks; these tests catch SQL/typing drift against the
migration that was just applied.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from runtime.ingress import (
    CertificateBudgetExceeded,
    CertificateService,
    DomainService,
    FakeCertificateIssuer,
)
from runtime.ingress.postgres import (
    PostgresCertificateStore,
    PostgresDomainStore,
    PostgresOutbox,
)

DSN = os.environ.get("DATABASE_URL", "")
APP_DSN = os.environ.get("APP_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="DATABASE_URL not configured")

NOW = datetime.now(timezone.utc)


def test_outbox_round_trip_and_idempotent_enqueue():
    async def scenario() -> None:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        topic = f"test.ingress.{uuid.uuid4().hex}"
        try:
            store = PostgresOutbox(conn)
            first = await store.enqueue(
                topic,
                "message-1",
                {"host": "acme.com"},
                now=NOW,
                max_attempts=3,
            )
            again = await store.enqueue(
                topic,
                "message-1",
                {"host": "ignored.example.com"},
                now=NOW,
            )
            assert first.id == again.id
            assert again.payload == {"host": "acme.com"}

            claimed = await store.claim(
                worker_id="worker-1", now=NOW, lease_seconds=30
            )
            assert claimed is not None
            assert claimed.topic == topic
            assert claimed.status == "running"
            assert claimed.attempts == 1

            done = await store.complete(
                claimed.id, worker_id="worker-1", now=NOW
            )
            assert done.status == "succeeded"
            assert done.worker_id is None
            assert done.lease_expires_at is None
        finally:
            await conn.execute("DELETE FROM ingress_outbox WHERE topic = $1", topic)
            await conn.close()

    asyncio.run(scenario())


def test_certificate_budget_is_enforced_atomically():
    async def scenario() -> None:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        registered = f"budget-{uuid.uuid4().hex}.example.com"
        try:
            store = PostgresCertificateStore(conn)
            for index in range(2):
                await store.reserve(
                    org_id=None,
                    registered_domain=registered,
                    hostnames=[f"host{index}.{registered}"],
                    is_wildcard=False,
                    idempotency_key=f"{registered}:{index}",
                    issuer="fake-acme",
                    now=NOW,
                    window=timedelta(days=7),
                    limit=2,
                )
            with pytest.raises(CertificateBudgetExceeded):
                await store.reserve(
                    org_id=None,
                    registered_domain=registered,
                    hostnames=[f"host2.{registered}"],
                    is_wildcard=False,
                    idempotency_key=f"{registered}:2",
                    issuer="fake-acme",
                    now=NOW,
                    window=timedelta(days=7),
                    limit=2,
                )
        finally:
            await conn.execute(
                "DELETE FROM ingress_certificates WHERE registered_domain = $1",
                registered,
            )
            await conn.close()

    asyncio.run(scenario())


def test_wildcard_is_reused_by_the_postgres_ledger():
    async def scenario() -> None:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        preview = f"preview-{uuid.uuid4().hex}.example.com"
        registered = ".".join(preview.split(".")[-2:])
        try:
            store = PostgresCertificateStore(conn)
            issuer = FakeCertificateIssuer(clock=lambda: NOW)
            service = CertificateService(store, issuer, clock=lambda: NOW)
            issued = []
            for _ in range(20):
                issued.append(await service.ensure_wildcard(preview))
            assert len(issuer.calls) == 1
            assert {record.id for record in issued} == {issued[0].id}
            assert issued[0].hostnames == (f"*.{preview}",)
        finally:
            await conn.execute(
                "DELETE FROM ingress_certificates WHERE registered_domain = $1",
                registered,
            )
            await conn.close()

    asyncio.run(scenario())


def test_tenant_role_cannot_read_platform_ingress_ledgers():
    if not APP_DSN:
        pytest.skip("APP_DATABASE_URL not configured")

    async def scenario() -> None:
        import asyncpg

        conn = await asyncpg.connect(APP_DSN)
        try:
            for table in ("ingress_certificates", "ingress_outbox"):
                with pytest.raises(
                    asyncpg.exceptions.InsufficientPrivilegeError
                ):
                    await conn.fetch(f"SELECT * FROM {table} LIMIT 1")
        finally:
            await conn.close()

    asyncio.run(scenario())


def test_domain_onboarding_uses_the_real_tables():
    async def scenario() -> None:
        import asyncpg

        conn = await asyncpg.connect(DSN)
        suffix = uuid.uuid4().hex[:10]
        org_id = str(uuid.uuid4())
        project_id = str(uuid.uuid4())
        hostname = f"acme-{suffix}.example.com"
        try:
            await conn.execute(
                "INSERT INTO orgs (id, name, slug) VALUES ($1, $2, $3)",
                uuid.UUID(org_id),
                f"Test org {suffix}",
                f"test-org-{suffix}",
            )
            await conn.execute(
                """
                INSERT INTO projects
                    (id, org_id, name, slug, provider, provider_repo_id,
                     k8s_namespace)
                VALUES ($1, $2, $3, $4, 'github', $5, $6)
                """,
                uuid.UUID(project_id),
                uuid.UUID(org_id),
                "test project",
                f"test-project-{suffix}",
                int(suffix, 16),
                f"autopilot-{suffix}",
            )

            service = DomainService(
                PostgresDomainStore(conn),
                outbox=PostgresOutbox(conn),
                clock=lambda: NOW,
            )
            plan, domains, created = await service.onboard(
                hostname=hostname,
                org_id=org_id,
                project_id=project_id,
                namespace=f"autopilot-{suffix}",
                tokens={
                    hostname: "token-apex",
                    f"www.{hostname}": "token-www",
                },
                id_factory=lambda: str(uuid.uuid4()),
            )
            assert created is True
            assert plan.apex == hostname
            assert {domain.hostname for domain in domains} == {
                hostname,
                f"www.{hostname}",
            }
            assert all(domain.status == "pending" for domain in domains)

            stored = await PostgresDomainStore(conn).find(hostname)
            assert stored is not None
            assert stored.project_id == project_id
            route = await PostgresDomainStore(conn).route_for(f"www.{hostname}")
            assert route is not None
            assert route.kind == "redirect"
            assert route.target_path == f"https://{hostname}"
        finally:
            await conn.execute("DELETE FROM routes WHERE hostname LIKE $1", f"%{suffix}%")
            await conn.execute("DELETE FROM domains WHERE hostname LIKE $1", f"%{suffix}%")
            await conn.execute("DELETE FROM projects WHERE id = $1", uuid.UUID(project_id))
            await conn.execute("DELETE FROM orgs WHERE id = $1", uuid.UUID(org_id))
            await conn.close()

    asyncio.run(scenario())
