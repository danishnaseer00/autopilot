# paas/build — Worker A (SPEC-07 → 12)

You build the plane where untrusted code meets our infrastructure.

- `detection/` — SPEC-07 framework detection → output kinds → manifest suggestions
- `gitware/` — SPEC-08 webhook receiver, repo ownership verification
- `pipeline/` — SPEC-09 clone → detect → build → publish; **the sandbox**
  (privileged DinD container only; buildpacks `pack`, no Kaniko — ADR-2)
- `supplychain/` — SPEC-10 registry proxy (caching + denylist + audit), egress rules
- `queue/` — SPEC-11 workers claiming via Postgres `FOR UPDATE SKIP LOCKED` (ADR-9)
- `registry/` — SPEC-12 push/verify client for the in-cluster registry

Hard rules: tenant-controlled values reach a shell only as build arguments
inside the sandbox container (SPEC-02 R-1) — no exceptions, ever. Build logs go
to object storage, never stay in the database after retention. Tests: unit +
contract with containerised Postgres/Redis; sandbox behaviour itself is
verified by the coordinator's isolation suite (SPEC-38 §3), not on your laptop.

First action in your session: `git merge master` (see `../PARALLEL-PLAN.md`).
