# paas/shared — cross-plane contracts in code (Coordinator)

The one thing every track imports and nobody but the coordinator changes.

Contents land in Phase 0/1:

- `manifest/` — SPEC-06 app manifest as typed models + the pure validation
  function. Build plane validates it, the renderers consume it, RCA derives
  the dependency graph from it. One definition, three importers — that is the
  whole point of SPEC-06.
- `events/` — the synthetic observation record the AutoPilot dataset builds
  from (SPEC-31) and the agent emits to (SPEC-34), so the planes never invent
  their own envelope shape.
- `db/` — access helpers that make R-2 (tenant predicate on every query) the
  default and a missing predicate the lint failure (SPEC-38 S-9).

Rules: no I/O logic here beyond those helpers; no framework imports that
would make `build/` or `autopilot/` depend on FastAPI; every change is a
spec-driven change and gets its own migration or its own commit citing the spec.
