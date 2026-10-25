-- Local fixture projects have no provider repository, so they all carry
-- provider_repo_id = 0. SPEC-08 R-5's global uniqueness rule applies to real
-- provider repositories, not to the absence of one. Replace the table
-- constraint with a partial unique index so multiple local fixtures can exist
-- without inventing fake positive repo IDs that could collide with GitHub.
--
-- Rollback:
--   DROP INDEX projects_provider_repo_unique;
--   ALTER TABLE projects
--     ADD CONSTRAINT projects_provider_repo_unique
--     UNIQUE (provider, provider_repo_id);

ALTER TABLE projects
  DROP CONSTRAINT projects_provider_repo_unique;

CREATE UNIQUE INDEX projects_provider_repo_unique
  ON projects (provider, provider_repo_id)
  WHERE provider_repo_id > 0;
