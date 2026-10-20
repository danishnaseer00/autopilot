"""SPEC-03 acceptance-criteria tests against a live Postgres.

Requires:
  DATABASE_URL      -> autopilot_migrator DSN (migrations already applied:
                       `python paas/migrations/apply.py`)
  APP_DATABASE_URL  -> autopilot_app DSN (RLS subject)
  AUDIT_DATABASE_URL-> autopilot_audit DSN (optional checks)

If DATABASE_URL is absent the module skips: pytest runs everywhere, the schema
checks run wherever the compose stack is up (`paas/docker-compose.dev.yml`).
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid as uuidlib
from pathlib import Path

import pytest
import psycopg
from psycopg import conninfo
from psycopg import errors as pgerr

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="DATABASE_URL not configured"
)

MIGRATOR_DSN = os.environ.get("DATABASE_URL", "")
APP_DSN = os.environ.get("APP_DATABASE_URL", "")
AUDIT_DSN = os.environ.get("AUDIT_DATABASE_URL", "")

# anchor to the real filesystem before climbing: pytest can present a
# relative __file__
HERE = Path(__file__).resolve().parent
MIGRATIONS_DIR = HERE.parent.parent / "migrations"


@pytest.fixture(scope="module", autouse=True)
def _migrations_applied():
    """AC-1: applying twice is a no-op (tracked in schema_migrations)."""
    if not MIGRATOR_DSN:
        pytest.skip("DATABASE_URL not configured")
    for _ in range(2):
        proc = subprocess.run(
            [sys.executable, str(MIGRATIONS_DIR / "apply.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout


@pytest.fixture(scope="module")
def migrator():
    with psycopg.connect(MIGRATOR_DSN) as conn:
        yield conn


@pytest.fixture  # function-scoped: one fresh connection per test, so every
def app_roles():  # RLS/transaction test starts from clean session state
    with psycopg.connect(APP_DSN) as conn:
        yield conn


TENANT_TABLES = [
    "orgs",
    "memberships",
    "projects",
    "project_branches",
    "deployments",
    "build_jobs",
    "routes",
    "env_vars",
    "domains",
    "credentials",
    "subscriptions",
    "billing_events",
    "usage_daily",
    "images",
    "incidents",
    "incident_actions",
    "diagnoses",
]


# ===== provisioning helpers (run as migrator; BYPASSRLS is for setup only) ===

def _one(connection, sql, *params):
    value = connection.execute(sql, params if params else None).fetchone()
    connection.commit()
    if value is None:
        raise RuntimeError(f"no RETURNING row for: {sql}")
    return value[0]


def _provision_tenant(conn, tag: str) -> dict:
    # unique suffix: every pytest run re-inserts rows, so globally-unique
    # columns (org slugs, user emails) must never repeat across runs
    tag = f"{tag}-{uuidlib.uuid4().hex[:8]}"
    org_id = _one(conn,
        "INSERT INTO orgs (name, slug) VALUES (%s, %s) RETURNING id",
        f"Org {tag}", tag)

    user_ids = []
    for index, email in enumerate(
        (f"{tag}-owner@example.com", f"{tag}-peer@example.com")
    ):
        uid = _one(conn,
            "INSERT INTO users (email, display_name) VALUES (%s, %s) RETURNING id",
            email, email.split("@")[0])
        if index == 0:  # one membership per org: the owner (peer has none yet)
            _one(conn,
                "INSERT INTO memberships (org_id, user_id, role) "
                "VALUES (%s, %s, 'owner') RETURNING org_id",
                org_id, uid)
        user_ids.append(uid)

    project_id = _one(conn,
        "INSERT INTO projects (org_id, name, slug, provider, provider_repo_id, "
        "k8s_namespace) VALUES (%s, %s, %s, 'github', 9901, %s) RETURNING id",
        org_id, f"proj-{tag}", f"repo-{tag}", f"org-{tag}")

    # project_branches has NO id column: its key is (project_id, git_ref)
    branch_ref = _one(conn,
        "INSERT INTO project_branches (project_id, org_id, git_ref, environment) "
        "VALUES (%s, %s, 'refs/heads/main', 'production') RETURNING git_ref",
        project_id, org_id)

    deployment_id = _one(conn,
        "INSERT INTO deployments (project_id, org_id, git_sha, git_ref, environment, "
        "trigger_source, manifest, manifest_hash, build_image, build_env_keys) "
        "VALUES (%s, %s, %s, 'refs/heads/main', 'production', 'manual', '{}', "
        "'deadbeef', 'registry.build@digest', '{}') RETURNING id",
        project_id, org_id, f"sha:{tag}")

    build_job_id = _one(conn,
        "INSERT INTO build_jobs (deployment_id, org_id, project_id) "
        "SELECT d.id, d.org_id, d.project_id FROM deployments d "
        "WHERE d.id = %s RETURNING id",
        deployment_id)

    route_id = _one(conn,
        "INSERT INTO routes (org_id, project_id, hostname, deployment_id, revision) "
        "VALUES (%s, %s, %s, %s, 'rev-1') RETURNING id",
        org_id, project_id, f"{tag}.example.test", deployment_id)

    env_var_id = _one(conn,
        "INSERT INTO env_vars (org_id, project_id, key, ciphertext, data_key) "
        "VALUES (%s, %s, 'API_KEY', %s, %s) RETURNING id",
        org_id, project_id, b"ciphertext", b"datakey")

    domain_id = _one(conn,
        "INSERT INTO domains (org_id, project_id, hostname, verification_token) "
        "VALUES (%s, %s, %s, 'contract-txt-token') RETURNING id",
        org_id, project_id, f"{tag}-custom.example.test")

    credential_id = _one(conn,
        "INSERT INTO credentials (org_id, project_id, provider, ciphertext, data_key, "
        "scopes) VALUES (%s, %s, 'github', %s, %s, ARRAY['repo']) RETURNING id",
        org_id, project_id, b"token-ciphertext", b"datakey")

    subscription_id = _one(conn,
        "INSERT INTO subscriptions (org_id, plan_id) "
        "SELECT %s, id FROM plans WHERE is_default LIMIT 1 RETURNING org_id",
        org_id)

    billing_event_id = _one(conn,
        "INSERT INTO billing_events (org_id, stripe_event_id, event_type, "
        "occurred_at, payload) VALUES (%s, %s, 'invoice.paid', now(), '{}') "
        "RETURNING id",
        org_id, f"evt_{tag}")

    _one(conn,
        "INSERT INTO usage_daily (org_id, day) VALUES (%s, current_date) "
        "RETURNING org_id",
        org_id)

    image_id = _one(conn,
        "INSERT INTO images (org_id, project_id, deployment_id, tag, digest, "
        "registry, size_bytes, build_image_digest, manifest_hash) "
        "VALUES (%s, %s, %s, %s, %s, 'registry.local', 100, 'build@digest', "
        "'deadbeef') RETURNING id",
        org_id, project_id, deployment_id, f"{tag}:v1", f"sha256-{tag}00")

    incident_id = _one(conn,
        "INSERT INTO incidents (org_id, project_id, deployment_id, status, "
        "severity, detected_at, detected_by) "
        "VALUES (%s, %s, %s, 'open', 'high', now(), 'threshold') RETURNING id",
        org_id, project_id, deployment_id)

    action_id = _one(conn,
        "INSERT INTO incident_actions (incident_id, org_id, action, target, status) "
        "VALUES (%s, %s, 'restart_service', 'web', 'proposed') RETURNING id",
        incident_id, org_id)

    diagnosis_id = _one(conn,
        "INSERT INTO diagnoses (incident_id, org_id, summary, confidence, candidates, "
        "claims, model_version, corpus_version) "
        "VALUES (%s, %s, 'cpu contention', 0.9, '[]', '[]', 'v0', 'c0') RETURNING id",
        incident_id, org_id)

    session_id = _one(conn,
        "INSERT INTO sessions (user_id, token_hash, expires_at, idle_expires_at) "
        "VALUES (%s, %s, now() + interval '1 day', now() + interval '1 day') "
        "RETURNING id",
        user_ids[0], uuidlib.uuid4().bytes)

    return {
        "org_id": org_id,
        "project_id": project_id,
        "deployment_id": deployment_id,
        "user_ids": user_ids,
        "rows": {
            "orgs": org_id,
            "memberships": org_id,
            "projects": project_id,
            "project_branches": branch_ref,
            "deployments": deployment_id,
            "build_jobs": build_job_id,
            "routes": route_id,
            "env_vars": env_var_id,
            "domains": domain_id,
            "credentials": credential_id,
            "subscriptions": subscription_id,
            "billing_events": billing_event_id,
            "usage_daily": org_id,
            "images": image_id,
            "incidents": incident_id,
            "incident_actions": action_id,
            "diagnoses": diagnosis_id,
        },
    }


@pytest.fixture(scope="module")
def org_a(migrator):
    return _provision_tenant(migrator, "orga-contract")


@pytest.fixture(scope="module")
def org_b(migrator):
    return _provision_tenant(migrator, "orgb-contract")


def _expect_violation(conn, sql, error, *params):
    """Run a statement that must be rejected by the database; roll the
    connection out of the aborted transaction state either way."""
    try:
        with pytest.raises(error):
            conn.execute(sql, params if params else None)
    finally:
        conn.rollback()


def _count(conn, table: str) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# ===== AC-1: idempotent migration runs ======================================

def test_ac1_migrations_are_idempotent():
    proc = subprocess.run(
        [sys.executable, str(MIGRATIONS_DIR / "apply.py")],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    assert "applied:" not in proc.stdout  # nothing left pending


# ===== AC-4: the app role has no BYPASSRLS ==================================

def test_ac4_app_role_has_no_bypassrls(app_roles):
    value = app_roles.execute(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname='autopilot_app'"
    ).fetchone()
    assert value == (False,)


# ===== AC-2 / AC-3: tenant isolation across the org boundary ================

def test_ac3_without_context_every_tenant_table_is_empty(app_roles):
    for table in TENANT_TABLES:
        assert _count(app_roles, table) == 0, table
    assert _count(app_roles, "sessions") == 0  # no app.user_id set either


def test_ac2_with_org_a_context_only_org_a_rows_visible(app_roles, org_a, org_b):
    conn = app_roles
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.org_id', %s, true)", (str(org_a["org_id"]),)
        )
        for table in TENANT_TABLES:
            assert _count(conn, table) == 1, table
        # sessions are user-scoped: the org context alone shows nothing
        assert _count(conn, "sessions") == 0


def test_ac2b_org_b_context_is_a_completely_different_view(app_roles, org_a, org_b):
    conn = app_roles
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.org_id', %s, true)", (str(org_b["org_id"]),)
        )
        for table in TENANT_TABLES:
            assert _count(conn, table) == 1, table


def test_ac5_recycled_connection_cannot_leak_context(app_roles, org_a):
    """set_config is transaction-local (R-3): back on the SAME connection,
    transaction 2 must start with an empty context (AC-5)."""
    conn = app_roles
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.org_id', %s, true)", (str(org_a["org_id"]),)
        )
        assert _count(conn, "orgs") == 1
    # transaction 2, same connection: task-local context is gone
    with conn.transaction():
        value = conn.execute(
            "SELECT current_setting('app.org_id', true)"
        ).fetchone()[0]
        assert value in (None, ""), "tenant context leaked across transactions"
        assert _count(conn, "orgs") == 0


def test_ac15_sessions_need_user_context(app_roles, org_a):
    conn = app_roles
    with conn.transaction():
        conn.execute(
            "SELECT set_config('app.org_id', %s, true)", (str(org_a["org_id"]),)
        )
        conn.execute(
            "SELECT set_config('app.user_id', %s, true)", (str(org_a["user_ids"][0]),)
        )
        assert _count(conn, "sessions") == 1


# ===== invariants enforced by the database ==================================

def test_ac6_ready_without_revision_rejected(migrator, org_a):
    _expect_violation(
        migrator,
        "INSERT INTO deployments (project_id, org_id, git_sha, git_ref, "
        "environment, trigger_source, manifest, manifest_hash, build_image, "
        "build_env_keys, status) "
        "VALUES (%s, %s, 'sha:r', 'r', 'production', 'manual', '{}', 'h', "
        "'build@x', '{}', 'ready')",
        pgerr.CheckViolation,
        org_a["project_id"], org_a["org_id"],
    )


def test_ac7_duplicate_stripe_event_rejected(migrator, org_a):
    event_id = f"dup-event-{uuidlib.uuid4().hex[:8]}"
    _one(migrator,
        "INSERT INTO billing_events (org_id, stripe_event_id, event_type, "
        "occurred_at, payload) VALUES (%s, %s, 'invoice.paid', now(), '{}') "
        "RETURNING id",
        org_a["org_id"], event_id)
    _expect_violation(
        migrator,
        "INSERT INTO billing_events (org_id, stripe_event_id, event_type, "
        "occurred_at, payload) VALUES (%s, %s, 'invoice.paid', now(), '{}')",
        pgerr.UniqueViolation,
        org_a["org_id"], event_id,
    )


def test_ac8_active_without_cert_rejected(migrator, org_a):
    _expect_violation(
        migrator,
        "UPDATE domains SET status='active', cert_issued_at=NULL WHERE org_id=%s",
        pgerr.CheckViolation,
        org_a["org_id"],
    )


def test_ac13_only_three_actions_representable(migrator, org_a):
    _expect_violation(
        migrator,
        "INSERT INTO incident_actions (incident_id, org_id, action, target, "
        "status) VALUES (%s, %s, 'delete_everything', 'web', 'proposed')",
        pgerr.CheckViolation,
        org_a["rows"]["incidents"], org_a["org_id"],
    )


def test_ac14_unverified_resolution_rejected(migrator, org_a):
    _expect_violation(
        migrator,
        "UPDATE incidents SET status='resolved', resolved_at=now(), "
        "resolved_by='agent', recovery_verified=false WHERE org_id=%s",
        pgerr.CheckViolation,
        org_a["org_id"],
    )
    # the corrected path goes through
    _one(migrator,
        "UPDATE incidents SET status='resolved', resolved_at=now(), "
        "resolved_by='agent', recovery_verified=true WHERE org_id=%s RETURNING id",
        org_a["org_id"])


# ===== schema-shape checks (SPEC-38 security gate S-5 precursor) =============

def test_every_tenant_table_carries_data_class(migrator):
    rows = migrator.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE column_name='data_class' AND table_schema='public'"
    ).fetchall()
    have = {r[0] for r in rows}
    expected = set(TENANT_TABLES) | {"plans", "route_history"}
    missing = expected - have
    assert not missing, f"tables missing data_class: {sorted(missing)}"


def test_data_class_values_are_constrained(migrator):
    value = migrator.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname LIKE '%data_class_check%' LIMIT 1"
    ).fetchone()
    assert value is not None and "C1" in value[0]


def test_rls_enabled_and_forced(migrator):
    rows = migrator.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='r'"
    ).fetchall()
    checked = {n: (r, f) for n, r, f in rows if n in TENANT_TABLES}
    assert set(checked) == set(TENANT_TABLES), "sanity: tenant tables exist"
    offenders = {n for n, (rls, force) in checked.items() if not (rls and force)}
    assert not offenders, f"RLS not enabled+forced on: {sorted(offenders)}"


def test_ac15_sessions_store_only_the_hash(migrator):
    rows = migrator.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='sessions' AND column_name ILIKE '%token%'"
    ).fetchall()
    assert [r[0] for r in rows] == ["token_hash"]


def test_project_branches_carries_org_id(migrator):
    value = migrator.execute(
        "SELECT is_nullable FROM information_schema.columns "
        "WHERE table_name='project_branches' AND column_name='org_id'"
    ).fetchone()
    assert value == ("NO",)


# ===== audit database rule (SPEC-03 §3) =====================================

@pytest.mark.skipif(not (APP_DSN and AUDIT_DSN), reason="audit DSNs not configured")
def test_audit_db_writable_by_audit_role_not_app():
    with psycopg.connect(AUDIT_DSN) as conn:
        conn.execute(
            "INSERT INTO audit_log (actor_type, actor_id, action, metadata) "
            "VALUES ('system', 'contract-test', 'audit.smoke', '{}')"
        )
        conn.commit()
        writable = conn.execute(
            "SELECT has_table_privilege(current_user, 'audit_log', 'DELETE') "
            "OR has_table_privilege(current_user, 'audit_log', 'UPDATE')"
        ).fetchone()[0]
        assert not writable, "audit_log must be append-only"
    app_on_audit = conninfo.make_conninfo(
        **{**conninfo.conninfo_to_dict(APP_DSN), "dbname": "autopilot_audit"}
    )
    with psycopg.connect(app_on_audit) as conn:
        has_privileges = conn.execute(
            "SELECT has_table_privilege(current_user, 'audit_log', 'DELETE') "
            "OR has_table_privilege(current_user, 'audit_log', 'UPDATE') "
            "OR has_table_privilege(current_user, 'audit_log', 'INSERT')"
        ).fetchone()[0]
        assert not has_privileges, "the app role must have NO audit_log privileges"
