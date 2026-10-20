-- target-db: autopilot_platform
-- SPEC-03 §4 baseline schema: types, tables, invariants, grants.
-- Applied as autopilot_migrator (the only DDL role; SPEC-03 R-2).
--
-- Findings vs the spec text, logged in DECISIONS.md (2026-10-20):
--   * data_class is baked into CREATE TABLE: §4.15 writes ALTERs because it
--     assumes an evolved database; this is the initial migration. Every table
--     also carries CHECK (data_class IN ('C1','C2','C3','C4')) so a bad class
--     is a database error, not a review catch. Tables not listed in §4.15
--     default to 'C3' (operational).
--   * project_branches carries org_id (amended §4.2) so R-1 ("every table
--     holding tenant data has RLS") holds without a per-row subquery.
--   * incidents carries resolved_requires_recovery: AC-14 requires the
--     database to reject resolved rows with recovery_verified=false; the
--     spec's CREATE TABLE omitted the CHECK that its own AC-14 demands.

-- ===== enum types (SPEC-03 §4.3/§4.4/§4.5/§4.6) =============================

CREATE TYPE deployment_status AS ENUM
  ('queued','building','ready','live','failed','cancelled','superseded');

CREATE TYPE job_status AS ENUM
  ('pending','running','succeeded','failed','cancelled','dead');

CREATE TYPE route_kind AS ENUM
  ('proxy','static','redirect','rewrite','notfound');

CREATE TYPE secret_target AS ENUM ('build','runtime','both');

-- ===== identity and tenancy ================================================

CREATE TABLE plans (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  code               TEXT UNIQUE NOT NULL,
  name               TEXT NOT NULL,
  price_cents        INT NOT NULL DEFAULT 0,
  stripe_price_id    TEXT,
  limits             JSONB NOT NULL,          -- shape in SPEC-10
  is_default         BOOLEAN NOT NULL DEFAULT false,
  version            INT NOT NULL DEFAULT 1,  -- optimistic concurrency (SPEC-02 F-8)
  data_class         TEXT NOT NULL DEFAULT 'C3'
                       CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- orgs: the tenant boundary (SPEC-02 Q-1)
CREATE TABLE orgs (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name         TEXT NOT NULL,
  slug         TEXT UNIQUE NOT NULL,
  plan_id      UUID REFERENCES plans(id),
  status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','suspended','closed')),
  data_class   TEXT NOT NULL DEFAULT 'C3'
                 CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         CITEXT UNIQUE NOT NULL,
  github_id     BIGINT UNIQUE,
  display_name  TEXT NOT NULL,
  password_hash TEXT,                     -- null when GitHub-only auth
  is_platform_admin BOOLEAN NOT NULL DEFAULT false,
  data_class    TEXT NOT NULL DEFAULT 'C2'  -- personal data (SPEC-29 C2)
                  CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at TIMESTAMPTZ
);

-- a user may belong to several orgs (SPEC-02 Q-1 default)
CREATE TABLE memberships (
  org_id    UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
  user_id   UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role      TEXT NOT NULL CHECK (role IN ('owner','admin','member')),
  data_class TEXT NOT NULL DEFAULT 'C3'
               CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (org_id, user_id)
);

-- ===== projects and connections (SPEC-03 §4.2) =============================

CREATE TABLE projects (
  id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id              UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,

  name                TEXT NOT NULL,
  slug                TEXT NOT NULL,
  provider            TEXT NOT NULL CHECK (provider IN ('github','gitlab')),
  provider_repo_id    BIGINT NOT NULL,    -- NOT the name: repos get renamed/transferred
  default_branch      TEXT NOT NULL DEFAULT 'main',

  -- encrypted at rest (SPEC-18); FK added after credentials exists
  clone_credential_id UUID,
  webhook_secret_enc  BYTEA,

  k8s_namespace       TEXT NOT NULL,      -- one namespace per ORG, not per project
  status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','suspended')),
  data_class          TEXT NOT NULL DEFAULT 'C3'
                        CHECK (data_class IN ('C1','C2','C3','C4')),

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (org_id, slug)
);
CREATE INDEX ON projects (provider, provider_repo_id);

-- what a push to a ref means for this project
-- org_id denormalised for RLS (SPEC-03 §5 R-1; amended §4.2)
CREATE TABLE project_branches (
  project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  org_id       UUID NOT NULL,
  git_ref      TEXT NOT NULL,
  environment  TEXT NOT NULL CHECK (environment IN ('production','preview')),
  auto_deploy  BOOLEAN NOT NULL DEFAULT true,
  data_class   TEXT NOT NULL DEFAULT 'C3'
                 CHECK (data_class IN ('C1','C2','C3','C4')),
  PRIMARY KEY (project_id, git_ref)
);

-- ===== deployments (SPEC-03 §4.3) ==========================================

CREATE TABLE deployments (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id        UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  org_id            UUID NOT NULL,        -- denormalised for RLS; see §5

  git_sha           TEXT NOT NULL,        -- identity. NEVER a branch name
  git_ref           TEXT NOT NULL,
  environment       TEXT NOT NULL,
  triggered_by      UUID REFERENCES users(id),
  trigger_source    TEXT NOT NULL
                      CHECK (trigger_source IN ('webhook','manual','api','agent')),

  status            deployment_status NOT NULL DEFAULT 'queued',
  stage             TEXT,                 -- finer than status, for log UX

  -- the four inputs, recorded so this build is reproducible
  manifest          JSONB NOT NULL,       -- resolved app manifest (SPEC-06/07)
  manifest_hash     TEXT NOT NULL,
  build_image       TEXT NOT NULL,        -- digest, never a tag (SPEC-09)
  build_env_keys    TEXT[] NOT NULL,      -- NAMES only, never values (SPEC-02 R-5)

  revision          TEXT,                 -- set when the image is pushed
  image_digest      TEXT,

  exit_code         INT,
  error_message     TEXT,
  log_uri           TEXT,

  data_class        TEXT NOT NULL DEFAULT 'C3'
                      CHECK (data_class IN ('C1','C2','C3','C4')),

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at        TIMESTAMPTZ,
  finished_at       TIMESTAMPTZ,
  superseded_by     UUID REFERENCES deployments(id),

  CONSTRAINT ready_has_revision
    CHECK (status <> 'ready' OR revision IS NOT NULL),
  CONSTRAINT terminal_has_finished
    CHECK (status NOT IN ('failed','cancelled','superseded') OR finished_at IS NOT NULL)
);
CREATE INDEX ON deployments (project_id, created_at DESC);
CREATE INDEX ON deployments (org_id, created_at DESC);
CREATE INDEX ON deployments (project_id, git_sha) WHERE status IN ('ready','live');

-- ===== build jobs: the queue (SPEC-03 §4.4) ================================

CREATE TABLE build_jobs (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  deployment_id   UUID NOT NULL REFERENCES deployments(id) ON DELETE CASCADE,
  org_id          UUID NOT NULL,
  project_id      UUID NOT NULL,

  status          job_status NOT NULL DEFAULT 'pending',
  priority        INT NOT NULL DEFAULT 0,
  attempts        INT NOT NULL DEFAULT 0,
  max_attempts    INT NOT NULL DEFAULT 3,

  available_at    TIMESTAMPTZ NOT NULL DEFAULT now(),   -- backoff and deferral
  worker_id       TEXT,
  heartbeat_at    TIMESTAMPTZ,
  lease_expires_at TIMESTAMPTZ,           -- SPEC-02 F-1 lease, never block
  last_error      TEXT,

  data_class      TEXT NOT NULL DEFAULT 'C3'
                    CHECK (data_class IN ('C1','C2','C3','C4')),

  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- the claim query's covering index (SPEC-02 F-1 / ADR-9: SKIP LOCKED)
CREATE INDEX ON build_jobs (status, priority DESC, available_at)
  WHERE status = 'pending';
CREATE INDEX ON build_jobs (lease_expires_at)
  WHERE status = 'running';

-- ===== routing: the only control/data plane interface (§4.5) ===============

CREATE TABLE routes (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID NOT NULL,
  project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  hostname      TEXT NOT NULL,
  path_prefix   TEXT NOT NULL DEFAULT '/',
  deployment_id UUID REFERENCES deployments(id),
  revision      TEXT,
  kind          route_kind NOT NULL DEFAULT 'proxy',
  target_path   TEXT,
  status_code   INT NOT NULL DEFAULT 200,
  priority      INT NOT NULL DEFAULT 0,

  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4')),

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (hostname, path_prefix)
);
CREATE INDEX ON routes (hostname);

-- append-only. "when did this break" must be answerable.
-- Platform-append-only: written during promotion/org-scoped inserts, no RLS
-- (finding logged: rows derive their org via hostname, not via org_id).
CREATE TABLE route_history (
  id            BIGSERIAL PRIMARY KEY,
  hostname      TEXT NOT NULL,
  from_revision TEXT,
  to_revision   TEXT,
  deployment_id UUID,
  actor         TEXT NOT NULL,
  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON route_history (hostname, created_at DESC);

-- ===== secrets metadata: ciphertext lives encrypted (§4.6, SPEC-18) ========

CREATE TABLE env_vars (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID NOT NULL,
  project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  key           TEXT NOT NULL,
  ciphertext    BYTEA NOT NULL,
  data_key      BYTEA NOT NULL,           -- encrypted under the org's DEK
  target        secret_target NOT NULL DEFAULT 'runtime',
  environments  TEXT[] NOT NULL DEFAULT '{production}',
  last_used_at  TIMESTAMPTZ,
  data_class    TEXT NOT NULL DEFAULT 'C1'  -- credential class (§4.15)
                  CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (project_id, key)
);

-- ===== domains (SPEC-03 §4.7, SPEC-15) =====================================

CREATE TABLE domains (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID NOT NULL,
  project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  hostname      TEXT NOT NULL UNIQUE,
  is_production BOOLEAN NOT NULL DEFAULT false,
  status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','verified','active','failed')),
  verification_token TEXT NOT NULL,       -- TXT record proof (SPEC-15)
  cert_issued_at    TIMESTAMPTZ,
  cert_expires_at   TIMESTAMPTZ,          -- SPEC-15 alert source
  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),

  -- an unverified domain must never receive traffic
  CONSTRAINT active_requires_verified
    CHECK (status <> 'active' OR cert_issued_at IS NOT NULL)
);

-- ===== plans, quotas, billing (§4.8) =======================================

CREATE TABLE subscriptions (
  org_id             UUID PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
  plan_id            UUID NOT NULL REFERENCES plans(id),
  stripe_customer_id TEXT,
  stripe_subscription_id TEXT,
  status             TEXT NOT NULL DEFAULT 'active'
                       CHECK (status IN ('trialing','active','past_due','canceled')),
  current_period_end TIMESTAMPTZ,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  version            INT NOT NULL DEFAULT 1,
  data_class         TEXT NOT NULL DEFAULT 'C3'
                       CHECK (data_class IN ('C1','C2','C3','C4'))
);

-- immutable ledger; billing state is derived from this, never updated in place
CREATE TABLE billing_events (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL,
  stripe_event_id   TEXT NOT NULL UNIQUE, -- idempotency (SPEC-02 F-7)
  event_type        TEXT NOT NULL,
  amount_cents      INT,
  currency          TEXT NOT NULL DEFAULT 'usd',
  occurred_at       TIMESTAMPTZ NOT NULL,
  payload           JSONB NOT NULL,       -- for dispute evidence
  processed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  data_class        TEXT NOT NULL DEFAULT 'C3'
                      CHECK (data_class IN ('C1','C2','C3','C4'))
);
CREATE INDEX ON billing_events (org_id, occurred_at DESC);

-- ===== usage rollups: shared by quota, inventory, reports (§4.9) ===========

CREATE TABLE usage_daily (
  org_id              UUID NOT NULL,
  day                 DATE NOT NULL,
  builds_succeeded    INT NOT NULL DEFAULT 0,
  builds_failed       INT NOT NULL DEFAULT 0,
  build_seconds       BIGINT NOT NULL DEFAULT 0,
  runtime_seconds     BIGINT NOT NULL DEFAULT 0,
  container_starts    INT NOT NULL DEFAULT 0,
  requests_total      BIGINT NOT NULL DEFAULT 0,
  egress_bytes        BIGINT NOT NULL DEFAULT 0,
  storage_bytes       BIGINT NOT NULL DEFAULT 0,
  incidents_opened    INT NOT NULL DEFAULT 0,
  incidents_resolved  INT NOT NULL DEFAULT 0,
  data_class          TEXT NOT NULL DEFAULT 'C3'
                        CHECK (data_class IN ('C1','C2','C3','C4')),
  PRIMARY KEY (org_id, day)
);
CREATE INDEX ON usage_daily (day);

-- ===== sessions and credentials (§4.11, SPEC-05) ===========================

-- Opaque server-side sessions. NOT JWTs: they cannot be revoked.
CREATE TABLE sessions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash    BYTEA NOT NULL UNIQUE,    -- never store the token (AC-15)
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ NOT NULL,     -- absolute
  idle_expires_at TIMESTAMPTZ NOT NULL,   -- idle
  revoked_at    TIMESTAMPTZ,
  revoke_reason TEXT,                     -- not null when revoked
  ip            INET,
  user_agent    TEXT,
  data_class    TEXT NOT NULL DEFAULT 'C2'  -- personal data (§4.15)
                  CHECK (data_class IN ('C1','C2','C3','C4'))
);
CREATE INDEX ON sessions (user_id) WHERE revoked_at IS NULL;

-- Encrypted provider tokens. Per-project scope, never one org-wide token.
CREATE TABLE credentials (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id       UUID NOT NULL,
  project_id   UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  provider     TEXT NOT NULL,
  ciphertext   BYTEA NOT NULL,
  data_key     BYTEA NOT NULL,
  scopes       TEXT[] NOT NULL,
  last_validated_at TIMESTAMPTZ,
  data_class   TEXT NOT NULL DEFAULT 'C1'  -- credential class (§4.15)
                 CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- resolve the projects <-> credentials cycle declared in §4.2/§4.11
ALTER TABLE projects
  ADD CONSTRAINT projects_clone_credential_id_fkey
  FOREIGN KEY (clone_credential_id) REFERENCES credentials(id);

-- ===== images (§4.12, SPEC-12) =============================================

CREATE TABLE images (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL,
  project_id        UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deployment_id     UUID REFERENCES deployments(id) ON DELETE SET NULL,
  tag               TEXT NOT NULL,          -- human handle
  digest            TEXT NOT NULL UNIQUE,   -- the identity
  registry          TEXT NOT NULL,
  size_bytes        BIGINT NOT NULL,
  build_image_digest TEXT NOT NULL,         -- SPEC-09 R-23: digest, not tag
  manifest_hash     TEXT NOT NULL,          -- explains the image without the registry
  marked_for_deletion_at TIMESTAMPTZ,       -- two-phase GC (SPEC-12 R-7)
  data_class        TEXT NOT NULL DEFAULT 'C3'
                      CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON images (org_id, created_at DESC);
CREATE INDEX ON images (marked_for_deletion_at) WHERE marked_for_deletion_at IS NOT NULL;

-- ===== build-script exceptions (§4.13, SPEC-10) ============================

-- Audited permission for a package to run install scripts.
CREATE TABLE build_script_exceptions (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  package_name  TEXT NOT NULL,
  granted_by    UUID NOT NULL REFERENCES users(id),
  reason        TEXT NOT NULL,
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at    TIMESTAMPTZ,              -- time-boxed
  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4'))
);
CREATE UNIQUE INDEX ON build_script_exceptions (project_id, package_name);

-- ===== AutoPilot domain objects (§4.14) ====================================
-- Written only by the AutoPilot service identity (SPEC-30+).

CREATE TABLE incidents (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id            UUID NOT NULL,
  project_id        UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  deployment_id     UUID,
  status            TEXT NOT NULL CHECK (status IN
                      ('open','diagnosed','actioning','resolved','escalated')),
  severity          TEXT NOT NULL,
  detected_at       TIMESTAMPTZ NOT NULL,
  detected_by       TEXT NOT NULL,        -- 'isolation_forest'|'lstm'|'threshold'|'manual'
  fault_signature   TEXT,                 -- present only for injected/known faults
  resolved_at       TIMESTAMPTZ,
  resolved_by       TEXT,                 -- 'agent'|'user'|'autoheal'|'null'
  -- SPEC-34 R-17: an action that ran and failed is NOT resolved
  recovery_verified BOOLEAN NOT NULL DEFAULT false,
  data_class        TEXT NOT NULL DEFAULT 'C3'  -- the research record (§4.15)
                      CHECK (data_class IN ('C1','C2','C3','C4')),

  -- AC-14: a row cannot claim 'resolved' unless recovery was verified
  CONSTRAINT resolved_requires_recovery
    CHECK (status <> 'resolved' OR recovery_verified = true)
);
CREATE INDEX ON incidents (org_id, detected_at DESC);

CREATE TABLE incident_actions (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id    UUID NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
  org_id         UUID NOT NULL,
  action         TEXT NOT NULL CHECK (action IN ('restart_service','rollback','scale')),
  target         TEXT NOT NULL,
  params         JSONB NOT NULL DEFAULT '{}',
  status         TEXT NOT NULL CHECK (status IN
                   ('proposed','pending_approval','approved','executed',
                    'verified','failed','rejected')),
  requires_approval BOOLEAN NOT NULL DEFAULT false,
  approved_by    UUID REFERENCES users(id),
  approved_at    TIMESTAMPTZ,
  approval_expires_at TIMESTAMPTZ,
  executed_at    TIMESTAMPTZ,
  audit_action_id UUID,                    -- SPEC-02 R-9, written before execution
  data_class     TEXT NOT NULL DEFAULT 'C3'
                   CHECK (data_class IN ('C1','C2','C3','C4'))
);
-- only three actions are representable; the CHECK is the enforcement (AC-13)

CREATE TABLE diagnoses (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  incident_id   UUID NOT NULL UNIQUE REFERENCES incidents(id) ON DELETE CASCADE,
  org_id        UUID NOT NULL,  -- denormalised for RLS (§5); SPEC-03 §4.14 amended
  summary       TEXT NOT NULL,
  confidence    REAL NOT NULL,
  candidates    JSONB NOT NULL,           -- SPEC-33 ranked list with evidence
  claims        JSONB NOT NULL,           -- evidence-bearing claims
  model_version TEXT NOT NULL,
  corpus_version TEXT NOT NULL,
  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== grants: answerable with role grants, not code review (§3) ===========
-- Roles were created by the deployment bootstrap (docker-init / node recipe):
--   autopilot_migrator - owns objects, BYPASSRLS, used only by apply.py
--   autopilot_app      - no BYPASSRLS; RLS policies own isolation
--   autopilot_audit    - owns autopilot_audit database writes only

GRANT USAGE ON SCHEMA public TO autopilot_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO autopilot_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO autopilot_app;

ALTER DEFAULT PRIVILEGES FOR ROLE autopilot_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO autopilot_app;
ALTER DEFAULT PRIVILEGES FOR ROLE autopilot_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO autopilot_app;
