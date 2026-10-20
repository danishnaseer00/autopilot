# paas/control — the control plane (Coordinator)

FastAPI application, ADR-4. Owner: the coordinator session — **except**
`app/billing/`, `app/reports/`, `app/notifications/`, which are Worker C's
(SPEC-21/22/24) once Phase 4 opens.

Landing order (matches BUILD-ORDER):

1. **Phase 0:** `app/` shell — FastAPI factory, `db.py` (async engine, RLS via
   `set local app.current_org`), `config.py` (Pydantic settings, no secrets in
   code — SPEC-18).
2. **Phase 0:** `auth/` — sessions, the 404-not-403 rule, developer/admin roles
   (SPEC-05). Structural; everything else codes against it.
3. **Phase 1:** `manifests/` — project manifest endpoints on top of
   `paas/shared/manifest/` (SPEC-06).
4. **Phase 2+:** `workloads/` — projects, deployments, promotion. Callers of the
   build queue; never of the build plane directly.
5. **Phase 4 (Worker C):** `billing/`, `reports/`, `notifications/` — each a
   self-contained module with its own routers and its own new migrations.

House rules: routers are thin, services hold logic, every query goes through
`db.py` helpers so the tenant predicate cannot be skipped. Health endpoint
before anything else — the AWS node's start-script checks it.
