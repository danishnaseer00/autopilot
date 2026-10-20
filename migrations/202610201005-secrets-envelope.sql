-- SPEC-18 schema findings (new migration; SPEC-03 §9 change control)
--
-- SPEC-18 §3.1 stores the per-org DEK in `orgs.dek_wrapped`, but the SPEC-03
-- baseline table has no such column. SPEC-18 R-11 needs a version the
-- deployment controller can compare, and R-12 needs a durable "this deployment
-- references a removed secret" marker. All three are additive, nullable/defaulted
-- columns: safe on a populated database, no backfill, no rewrite of existing
-- rows beyond the default (expand step; no contract step needed).
--
-- Proposed SPEC-03 amendments (logged in DECISIONS.md, 2026-10-20):
--   * §4.1 orgs.dek_wrapped            BYTEA NULL (created on first secret write)
--   * §4.2 projects.secrets_version    INT NOT NULL DEFAULT 0
--   * §4.3 deployments.secrets_version INT NOT NULL DEFAULT 0
--   * §4.3 deployments.stale_secrets   BOOLEAN NOT NULL DEFAULT false
--
-- target-db: autopilot_platform

ALTER TABLE orgs
  ADD COLUMN dek_wrapped BYTEA;

ALTER TABLE projects
  ADD COLUMN secrets_version INT NOT NULL DEFAULT 0,
  ADD CONSTRAINT projects_secrets_version_nonnegative
    CHECK (secrets_version >= 0);

ALTER TABLE deployments
  ADD COLUMN secrets_version INT NOT NULL DEFAULT 0,
  ADD COLUMN stale_secrets BOOLEAN NOT NULL DEFAULT false,
  ADD CONSTRAINT deployments_secrets_version_nonnegative
    CHECK (secrets_version >= 0);

-- "which deployments need a rebuild after a secret was removed?"
CREATE INDEX deployments_stale_secrets_idx
  ON deployments (project_id)
  WHERE stale_secrets;
