# paas/ — the product code

**Everything that ships lives here.** Specs stay at the repo root (they are
contracts, not code). `deploy/` stays at the repo root (node provisioning +
platform manifests, infrastructure ops). This tree is code only.

Status: directories + READMEs only. Real files land when their spec's phase
opens — starting with SPEC-03 (schema) and SPEC-06 (manifest), which are the
coordinator's job, then the worker tracks fill their own directories.

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
