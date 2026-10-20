-- target-db: autopilot_platform
-- 2026-10-20 amendment batch: SPEC-08 persistence + SPEC-13 staged deploy
-- inputs. Expand-only: nullable columns, defaulted columns, new tables.
-- Rationale entries live in DECISIONS.md (SPEC-08 proposals (a)/(d)/(j)/(k),
-- SPEC-13 proposal 1).

-- ===== projects: ownership verification + provider identity ================
-- SPEC-08 needs the verification state the webhook gate reads, and the
-- statuses that stop builds (orphaned repo, revoked credential).
ALTER TABLE projects
  ADD COLUMN verification_token TEXT,
  ADD COLUMN verification_method TEXT
    CHECK (verification_method IN ('txt','branch')),
  ADD COLUMN verified_at TIMESTAMPTZ,
  ADD COLUMN verification_checked_at TIMESTAMPTZ,
  ADD COLUMN verification_error TEXT;

ALTER TABLE projects DROP CONSTRAINT projects_status_check;
ALTER TABLE projects ADD CONSTRAINT projects_status_check
  CHECK (status IN ('active','suspended','orphaned','credential_invalid'));

-- R-5 (SPEC-08): one secret per project assumes a repo maps to one project.
DROP INDEX IF EXISTS projects_provider_provider_repo_id_idx;
ALTER TABLE projects
  ADD CONSTRAINT projects_provider_repo_unique UNIQUE (provider, provider_repo_id);

-- ===== project_branches: deterministic rule order (SPEC-08 R-10) ===========
ALTER TABLE project_branches
  ADD COLUMN position INT NOT NULL DEFAULT 0;

-- ===== deployments: preview/delivery identity + staged inputs ==============
-- SPEC-13 inserts the row at step 1, before manifest resolution (step 3) and
-- builder-image selection (step 5). The four inputs are therefore filled
-- later; a ready-or-later row must still carry all three resolved values.
ALTER TABLE deployments
  ALTER COLUMN manifest DROP NOT NULL,
  ALTER COLUMN manifest_hash DROP NOT NULL,
  ALTER COLUMN build_image DROP NOT NULL,
  ADD COLUMN preview_hostname TEXT,
  ADD COLUMN pr_number INT CHECK (pr_number IS NULL OR pr_number > 0),
  ADD COLUMN delivery_id TEXT,
  ADD COLUMN matched_ref TEXT;

ALTER TABLE deployments ADD CONSTRAINT ready_has_build_inputs
  CHECK (status NOT IN ('ready','live','superseded')
         OR (manifest IS NOT NULL
             AND manifest_hash IS NOT NULL
             AND build_image IS NOT NULL));

-- ===== preview hostnames: stable per (project, ref) ========================
-- SPEC-08 AC-4: the hostname is allocated once and reused forever. A global
-- hostname uniqueness constraint is what the route reconciler (SPEC-16) needs.
CREATE TABLE preview_hostnames (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id      UUID NOT NULL,
  project_id  UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

  ref_kind    TEXT NOT NULL CHECK (ref_kind IN ('pr','branch')),
  ref_key     TEXT NOT NULL,             -- 'pr:123' or 'branch:feature-x'
  hostname    TEXT NOT NULL,

  data_class  TEXT NOT NULL DEFAULT 'C3'
                CHECK (data_class IN ('C1','C2','C3','C4')),

  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (project_id, ref_kind, ref_key),
  UNIQUE (hostname)
);
CREATE INDEX ON preview_hostnames (org_id);

ALTER TABLE preview_hostnames ENABLE ROW LEVEL SECURITY;
ALTER TABLE preview_hostnames FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON preview_hostnames
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== webhook deliveries: idempotency claims (SPEC-08) ====================
-- Claimed before dispatch (X-GitHub-Delivery). project_id/org_id are nullable
-- because an unknown repo with a valid signature is a recorded 202 no-op; the
-- migrator (BYPASSRLS) processes those, tenants see only their own rows.
CREATE TABLE webhook_deliveries (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id        UUID,
  project_id    UUID REFERENCES projects(id) ON DELETE CASCADE,

  provider      TEXT NOT NULL DEFAULT 'github'
                  CHECK (provider IN ('github','gitlab')),
  delivery_id   TEXT NOT NULL,
  event         TEXT,
  ref           TEXT,
  sha           TEXT,
  action        TEXT,                  -- accepted | ignored | blocked_unverified
  deployment_id UUID REFERENCES deployments(id) ON DELETE SET NULL,
  received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  data_class    TEXT NOT NULL DEFAULT 'C3'
                  CHECK (data_class IN ('C1','C2','C3','C4')),

  UNIQUE (provider, delivery_id)
);
CREATE INDEX ON webhook_deliveries (project_id, received_at DESC);

ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON webhook_deliveries
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());
