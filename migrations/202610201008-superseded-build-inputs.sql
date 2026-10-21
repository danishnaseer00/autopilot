-- target-db: autopilot_platform
-- SPEC-08/SPEC-09: superseded queued work predates a manifest/build image.
--
-- 202610201007 made the build inputs nullable until the build stage but kept
-- `superseded` in `ready_has_build_inputs`. A queued deployment that is replaced
-- by a newer push (SPEC-08 R-15) has no inputs yet, so transitioning it to
-- `superseded` violated the constraint. Superseding is a queue lifecycle move,
-- not a claim that the deployment was ever buildable; only `ready`/`live` must
-- carry the three resolved inputs.
--
-- NOT VALID keeps the migration safe if an older row already failed the prior
-- constraint; every new or updated row is still checked.

ALTER TABLE deployments DROP CONSTRAINT ready_has_build_inputs;

ALTER TABLE deployments ADD CONSTRAINT ready_has_build_inputs
  CHECK (status NOT IN ('ready','live')
         OR (manifest IS NOT NULL
             AND manifest_hash IS NOT NULL
             AND build_image IS NOT NULL))
  NOT VALID;
