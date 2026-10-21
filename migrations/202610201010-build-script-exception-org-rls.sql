-- target-db: autopilot_platform
-- SPEC-03 §4.13 / SPEC-10: build_script_exceptions shipped project-scoped with
-- no org_id and no RLS, unlike every other tenant table. The build plane is not
-- a tenant boundary process, so the database must enforce the scope.
--
-- Expand-only and backfilled: existing rows take their project's org; the
-- policy is the standard tenant_isolation pair used by 202610201003.

ALTER TABLE build_script_exceptions
  ADD COLUMN org_id UUID REFERENCES orgs(id) ON DELETE CASCADE;

UPDATE build_script_exceptions e
   SET org_id = p.org_id
  FROM projects p
 WHERE p.id = e.project_id;

ALTER TABLE build_script_exceptions ALTER COLUMN org_id SET NOT NULL;

CREATE INDEX build_script_exceptions_org_idx
  ON build_script_exceptions (org_id);

ALTER TABLE build_script_exceptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE build_script_exceptions FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON build_script_exceptions
  USING      (org_id = current_org_id())
  WITH CHECK (org_id = current_org_id());
