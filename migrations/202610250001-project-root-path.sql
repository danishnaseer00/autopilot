-- Persist the optional repository root path from SPEC-36 §3.2 (POST
-- /projects {org, name, root_path?}) and SPEC-07 Q-2. The console already
-- sends it; storing it keeps the project detail and build inputs honest.
--
-- Existing projects keep the repository-root default. Rollback:
--   ALTER TABLE projects DROP COLUMN root_path;

ALTER TABLE projects
  ADD COLUMN root_path TEXT NOT NULL DEFAULT '/';

ALTER TABLE projects
  ADD CONSTRAINT projects_root_path_absolute CHECK (root_path LIKE '/%');
