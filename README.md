# paas/ — the product code

**Everything that ships lives here.** Specs stay at the repo root (they are
contracts, not code). `deploy/` stays at the repo root (node provisioning +
platform manifests, infrastructure ops). This tree is code only.

- 2026-10-20 — **Coordinator role confirmed.** Workers code in their worktrees
  and stop-report; the coordinator reviews every diff against the spec's
  acceptance criteria, runs the SPEC-38 isolation suite + security gate on the
  merged state, and merges one branch at a time. Done = acceptance criteria
  passing, not vibes.
- 2026-10-20 — **Phase 0+1 landed (coordinator).** SPEC-03 baseline migrations
  (`paas/migrations/`), uv workspace (`paas/`), `platform_shared` manifest
  package (SPEC-06), FastAPI shell (`paas/control`), contract test suites
  (`paas/tests/contract/`, `paas/shared/tests/`), dev compose stack
  (`paas/docker-compose.dev.yml` + `docker-init/`). Spec amendments below.
- 2026-10-20 — **SPEC-03 findings applied (§9 process).**
  (a) `project_branches` gained `org_id NOT NULL` — R-1 needs the
  denormalised column for a fast RLS predicate, not a per-row subquery; spec
  §4.2 amended.
  (b) `incidents` gained `resolved_requires_recovery` CHECK — AC-14 demanded
  it but the CREATE TABLE omitted it; spec §4.14 amended.
  (c) `data_class` is baked into every CREATE TABLE (§4.15 wrote ALTERs for an
  evolved DB) and carries a `CHECK (data_class IN ('C1','C2','C3','C4'))` so
  a wrong class is a database error, not a review catch. Tables not listed in
  §4.15 default to `C3` (operational) — review when SPEC-10/12/13 land.
  (d) `route_history` stays platform-append-only without RLS (rows keyed by
  hostname); `users` is not RLS'd (platform-wide identity, no org column) —
  SPEC-05 must revisit when sessions/auth land.
  (e) `usage_daily` is a plain table for now; the TimescaleDB hypertable
  conversion is a later forward-only migration (extension optional at apply
  time, ADR-6 preserved).
- 2026-10-20 — **SPEC-06 route-edge interpretation (needs RCA sign-off, R-15).**
  The §3 fixture implies: route edges go from the service holding the `" /"`
  route to each other route's service (`/ → frontend, /api → backend` ⇒ edge
  `frontend → backend`, which reproduces AC-10 exactly). Implemented in
  `platform_shared.manifest.graph`. **Worker D must confirm this rule before
  SPEC-35 consumes the graph** — it is the only ambiguous point in the shared
  interface.
- 2026-10-20 — **Tooling: uv workspaces** under `paas/` (one shared .venv,
  editable member packages). `paas/pyproject.toml` is the virtual root;
  `uv sync` + `uv run pytest` are the only commands a session needs.

## Status

Phase 0+1 + SPEC-07, SPEC-08, SPEC-18, SPEC-13, SPEC-14, SPEC-09, SPEC-10 and
SPEC-15 landed. `shared/` (SPEC-06 manifest + canonical hash), `build/`
detection, git integration, the build pipeline and supply-chain controls,
`runtime/` secrets envelope, deployment lifecycle, Kubernetes
rendering/actions and the ingress/TLS certificate ledger + reconciler,
`migrations/` (SPEC-03) and `tests/contract/` are real. Migrations
`202610201007`-`1011` carry the SPEC-08 persistence tables, SPEC-13 staged
deploy inputs, SPEC-09 reproducibility columns, the SPEC-10 org/RLS fix for
`build_script_exceptions` and the SPEC-15 ingress/TLS ledger. In flight:
SPEC-11 (build plane, Worker A) and SPEC-16 (runtime plane, Worker B). Cluster
E2E and the fresh-volume apply (now through `1011`) are still owed by the
coordinator.

## Fast workflow (every session)

```bash
# one-time (or after pulling): create the shared venv
cd paas && uv sync

# dev databases for unit/contract tests (Docker Desktop, tiny footprint)
docker compose -f docker-compose.dev.yml up -d

# apply the schema (repeat after pulling new migration files)
uv run python migrations/apply.py

# run everything a session is responsible for
uv run pytest          # shared + control + tests/contract
```

Contract tests skip cleanly when `DATABASE_URL` is unset - set it from
`.env.example` (copy to `paas/.env`) to actually exercise the schema.

## Ownership map (binding — see `../PARALLEL-PLAN.md` rule 1)

| Directory | Owner | Specs | Status |
|---|---|---|---|
| `control/` | Coordinator (Worker C inside exactly `app/billing/`, `app/reports/`, `app/notifications/`) | 04 core, 05 auth, 21, 22, 24 | Phase 0/4 |
| `shared/` | Coordinator | 06 manifest types, cross-plane interfaces | Phase 0/1 |
| `build/` | Worker A | 07, 08, 09, 10, 11, 12 | Phase 2 |
| `runtime/` | Worker B | 18, 13, 14, 15, 16 | Phase 3 |
| `autopilot/` | Worker D | 30–35 | Phase 5, after vertical slice |
| `console/` | Worker C | 36 contract (read-only), 37 implementation | Phase 4 |
| `migrations/` | new files by any worker; content governed by SPEC-03 §9 | 03 | Phase 0 |
| `tests/` | Coordinator runs; all workers contribute their slice | 38 | all phases |

## Ground rules

1. Workers own their directory. Crossing into another directory is a
   spec-change proposal in `DECISIONS.md`, not an edit.
2. Stack is fixed by ADR: Python/FastAPI for `control`, `shared`, `build`
   worker, `runtime` reconciler, and `autopilot`. React/Vite for `console`.
3. Every module carries its unit tests next to it; `tests/` holds the
   cross-cutting suites (contract, isolation, security).
4. Commits cite the spec requirement: `feat(build): ... (SPEC-09 R-4)`.
5. Nothing here runs in the open without its acceptance criteria passing
   locally first (workers) and the SPEC-38 gates passing on merge (coordinator).
