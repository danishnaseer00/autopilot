-- target-db: autopilot_platform
-- SPEC-09 R-22: node version is one of the five reproducibility inputs and must
-- be recorded on the deployment, not only folded into a cache-key string.
--
-- Expand-only: existing rows stay NULL; new successful builds record the
-- resolved Node major (NULL for static/container/Buildpack-managed cases).

ALTER TABLE deployments
  ADD COLUMN node_version INT
    CHECK (node_version IS NULL OR node_version > 0);
