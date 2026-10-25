-- SPEC-05 session authentication and pre-tenant access resolution.
--
-- Why this exists: `sessions` is forced-RLS on `user_id = current_user_id()`,
-- but the API must find the user *from* a token before it knows the user. The
-- same problem applies to resolving an org/project/deployment id before the
-- tenant GUC can be set. The app role cannot bypass RLS, so these narrow
-- SECURITY DEFINER lookups are the only pre-tenant reads; each one is bounded
-- by the authenticated user id and returns only the caller's own rows.
--
-- Rollback: drop the functions. They hold no state.
-- Applied as autopilot_migrator (BYPASSRLS); execute is granted to autopilot_app.

CREATE OR REPLACE FUNCTION auth_lookup_session(p_token_hash bytea)
RETURNS TABLE (
    session_id          uuid,
    user_id             uuid,
    email               text,
    display_name        text,
    is_platform_admin   boolean,
    expires_at          timestamptz,
    idle_expires_at     timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT s.id,
           u.id,
           u.email::text,
           u.display_name,
           u.is_platform_admin,
           s.expires_at,
           s.idle_expires_at
      FROM sessions s
      JOIN users u ON u.id = s.user_id
     WHERE s.token_hash = p_token_hash
       AND s.revoked_at IS NULL
       AND s.expires_at > now()
       AND s.idle_expires_at > now()
$$;

CREATE OR REPLACE FUNCTION auth_touch_session(
    p_session_id uuid,
    p_idle_seconds integer
)
RETURNS void
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    UPDATE sessions
       SET last_seen_at = now(),
           idle_expires_at = now()
               + make_interval(secs => GREATEST(p_idle_seconds, 60))
     WHERE id = p_session_id
       AND revoked_at IS NULL
$$;

CREATE OR REPLACE FUNCTION auth_list_user_orgs(p_user_id uuid)
RETURNS TABLE (
    org_id              uuid,
    slug                text,
    name                text,
    role                text,
    org_status          text,
    plan_code           text,
    plan_name           text,
    plan_limits         jsonb,
    subscription_status text,
    current_period_end  timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT o.id,
           o.slug,
           o.name,
           m.role,
           o.status,
           p.code,
           p.name,
           COALESCE(p.limits, '{}'::jsonb),
           s.status,
           s.current_period_end
      FROM memberships m
      JOIN orgs o ON o.id = m.org_id
      LEFT JOIN plans p ON p.id = o.plan_id
      LEFT JOIN subscriptions s ON s.org_id = o.id
     WHERE m.user_id = p_user_id
     ORDER BY o.created_at, o.id
$$;

CREATE OR REPLACE FUNCTION auth_project_access(
    p_user_id uuid,
    p_project_id uuid
)
RETURNS TABLE (org_id uuid, role text, project_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT p.org_id, m.role, p.id
      FROM projects p
      JOIN memberships m ON m.org_id = p.org_id
     WHERE p.id = p_project_id
       AND m.user_id = p_user_id
$$;

CREATE OR REPLACE FUNCTION auth_deployment_access(
    p_user_id uuid,
    p_deployment_id uuid
)
RETURNS TABLE (org_id uuid, role text, project_id uuid, deployment_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT d.org_id, m.role, d.project_id, d.id
      FROM deployments d
      JOIN memberships m ON m.org_id = d.org_id
     WHERE d.id = p_deployment_id
       AND m.user_id = p_user_id
$$;

CREATE OR REPLACE FUNCTION auth_domain_access(
    p_user_id uuid,
    p_domain_id uuid
)
RETURNS TABLE (org_id uuid, role text, project_id uuid, domain_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT d.org_id, m.role, d.project_id, d.id
      FROM domains d
      JOIN memberships m ON m.org_id = d.org_id
     WHERE d.id = p_domain_id
       AND m.user_id = p_user_id
$$;

CREATE OR REPLACE FUNCTION auth_incident_access(
    p_user_id uuid,
    p_incident_id uuid
)
RETURNS TABLE (org_id uuid, role text, project_id uuid, incident_id uuid)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT i.org_id, m.role, i.project_id, i.id
      FROM incidents i
      JOIN memberships m ON m.org_id = i.org_id
     WHERE i.id = p_incident_id
       AND m.user_id = p_user_id
$$;

REVOKE ALL ON FUNCTION auth_lookup_session(bytea) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_touch_session(uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_list_user_orgs(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_project_access(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_deployment_access(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_domain_access(uuid, uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_incident_access(uuid, uuid) FROM PUBLIC;

GRANT EXECUTE ON FUNCTION auth_lookup_session(bytea) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_touch_session(uuid, integer) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_list_user_orgs(uuid) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_project_access(uuid, uuid) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_deployment_access(uuid, uuid) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_domain_access(uuid, uuid) TO autopilot_app;
GRANT EXECUTE ON FUNCTION auth_incident_access(uuid, uuid) TO autopilot_app;
