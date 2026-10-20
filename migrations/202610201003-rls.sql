-- target-db: autopilot_platform
-- SPEC-03 §5: row-level security on every tenant-scoped table.
-- org_id is denormalised onto rows that would otherwise need a join, so the
-- policy stays a per-row predicate, not a per-row subquery.
--
-- The application sets the tenant per transaction (SPEC-03 R-3):
--     BEGIN;
--     SELECT set_config('app.org_id', $1, true);
--     SELECT set_config('app.user_id', $2, true);   -- where a user is bound
--     COMMIT;
-- Transaction-local settings expire with the transaction (AC-5), so a pooled
-- connection cannot leak a tenant into the next request.
--
-- Why the *_id() functions: a GUC that was set with is_local=true comes back
-- as '' (not NULL) in later transactions on the same connection, and
-- ''::uuid is a hard 22P02 error - noisy 500s instead of fail-closed empty
-- results. current_org_id() / current_user_id() map NULL, '', and garbage to
-- NULL, so a request without tenant context sees ZERO ROWS everywhere (AC-3).
--
-- autopilot_migrator is exempt via BYPASSRLS (documented support task only);
-- autopilot_app has no such attribute (AC-4).

-- ===== context functions (fail-closed tenant resolution) ====================

CREATE OR REPLACE FUNCTION current_org_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $org$
DECLARE
    value TEXT;
BEGIN
    value := current_setting('app.org_id', true);
    IF value IS NULL OR value = '' THEN
        RETURN NULL;
    END IF;
    BEGIN
        RETURN value::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN NULL;
    END;
END;
$org$;

CREATE OR REPLACE FUNCTION current_user_id() RETURNS uuid
LANGUAGE plpgsql STABLE AS $usr$
DECLARE
    value TEXT;
BEGIN
    value := current_setting('app.user_id', true);
    IF value IS NULL OR value = '' THEN
        RETURN NULL;
    END IF;
    BEGIN
        RETURN value::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RETURN NULL;
    END;
END;
$usr$;

-- ===== org / membership / project ==========================================

ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
ALTER TABLE orgs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON orgs
  USING      (id = current_org_id())
  WITH CHECK (id = current_org_id());

ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON memberships
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON projects
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE project_branches ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_branches FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON project_branches
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== build / route / secret / domain =====================================

ALTER TABLE deployments ENABLE ROW LEVEL SECURITY;
ALTER TABLE deployments FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON deployments
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE build_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE build_jobs FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON build_jobs
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE routes FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON routes
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE env_vars ENABLE ROW LEVEL SECURITY;
ALTER TABLE env_vars FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON env_vars
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE domains ENABLE ROW LEVEL SECURITY;
ALTER TABLE domains FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON domains
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON credentials
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== billing / usage =====================================================

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON subscriptions
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_events FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON billing_events
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE usage_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE usage_daily FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON usage_daily
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== images ==============================================================

ALTER TABLE images ENABLE ROW LEVEL SECURITY;
ALTER TABLE images FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON images
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== AutoPilot objects ===================================================

ALTER TABLE incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE incidents FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON incidents
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE incident_actions ENABLE ROW LEVEL SECURITY;
ALTER TABLE incident_actions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON incident_actions
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

ALTER TABLE diagnoses ENABLE ROW LEVEL SECURITY;
ALTER TABLE diagnoses FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON diagnoses
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());

-- ===== user-scoped: sessions ===============================================
-- Sessions have no org_id (a user is a platform-wide identity); their policy
-- scopes rows by the request's user instead (AC-15 context).

ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY user_isolation ON sessions
  USING      (user_id = current_user_id())
  WITH CHECK (user_id = current_user_id());

-- route_history: platform-append-only timeline keyed by hostname; no RLS by
-- design (finding logged). users: platform identity table, no tenant column;
-- access mediated by application queries (SPEC-05 will review).
