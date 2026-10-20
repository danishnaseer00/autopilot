-- target-db: autopilot_audit
-- SPEC-03 §3/§4.10: audit_log lives in its own database, write-only from the
-- platform. Applied as autopilot_migrator on autopilot_audit.
--
-- Access is a role grant, not code review:
--   autopilot_audit role  - INSERT + SELECT on audit_log (platform writes here)
--   autopilot_app         - NO grant in this database at all
--   console read access   - granted to a read-only role in a later migration

CREATE TABLE audit_log (
  id          BIGSERIAL PRIMARY KEY,
  org_id      UUID,                     -- null for platform-level actions
  actor_type  TEXT NOT NULL CHECK (actor_type IN ('user','agent','system')),
  actor_id    TEXT NOT NULL,
  action      TEXT NOT NULL,            -- dotted verb: 'secret.read', 'route.promote'
  target_type TEXT,
  target_id   TEXT,
  metadata    JSONB NOT NULL DEFAULT '{}',
  ip          INET,
  data_class  TEXT NOT NULL DEFAULT 'C3'  -- §4.15: the audit record
                CHECK (data_class IN ('C1','C2','C3','C4')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON audit_log (org_id, created_at DESC);
CREATE INDEX ON audit_log (action, created_at DESC);

GRANT USAGE ON SCHEMA public TO autopilot_audit;
GRANT INSERT, SELECT ON audit_log TO autopilot_audit;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO autopilot_audit;

-- nobody else, on principle:
REVOKE ALL ON audit_log FROM PUBLIC;
