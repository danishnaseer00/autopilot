-- SPEC-13 lifecycle query indexes (new migration; SPEC-03 §9 change control)
--
-- No columns change. These indexes back the three hot predicates the lifecycle
-- service runs on every push/cutover:
--
--   * R-10 duplicate-delivery lookup: (project, sha, ref) while in flight;
--   * R-6/R-8 newer-wins and supersede scan: (project, ref, status, age);
--   * promotion/rollback route selection: (project, current deployment).
--
-- They are ordinary (non-concurrent) CREATE INDEX statements: the dev database
-- is small, and migrations are transactional. If a production table is already
-- large, the coordinator should apply the equivalent CONCURRENTLY outside this
-- forward-only migration (expand step).
--
-- target-db: autopilot_platform

CREATE INDEX deployments_idempotency_idx
  ON deployments (project_id, git_sha, git_ref, created_at DESC)
  WHERE status IN ('queued', 'building');

CREATE INDEX deployments_supersede_idx
  ON deployments (project_id, git_ref, status, created_at DESC);

CREATE INDEX routes_promotion_idx
  ON routes (project_id, deployment_id);

CREATE INDEX route_history_deployment_idx
  ON route_history (deployment_id, created_at DESC);
