# paas/migrations — governed by SPEC-03 §9 (content owned by Coordinator)

The single most contended artefact in the project. Rules that apply to every
worker:

1. **New files only.** Never edit an existing migration — SPEC-39 §4's
   expand/migrate/contract discipline makes old files immutable by definition.
2. Naming: `YYYYMMDDhhmm-<slug>.sql` (UTC timestamp when the file is written).
   Clashing timestamps = rebase conflict to resolve with the coordinator.
3. Every new table carries `data_class TEXT NOT NULL CHECK (...)` (SPEC-29 §3)
   and its RLS policy in the same file — a table shipped without RLS is a defect
   even if it is empty (SPEC-02 R-2).
4. Header comment in every file: what changes, which spec/requirement drove it,
   and the rollback (or "none; destructive migration with reviewed plan per
   SPEC-39 R-11").
5. Backfills are separate resumable jobs, never inlined in the migration
   (SPEC-39 R-9).
6. `{scratch}` — coordinator-owned: `extensions.sql` (pgcrypto, TimescaleDB),
   then the SPEC-03 base schema lands here first as `2026...-0001-baseline.sql`.
