# paas/runtime — Worker B (SPEC-18 → 13 → 14 → 15 → 16)

You render platform intents into Kubernetes and keep the data plane honest.

- `secrets/` — SPEC-18 envelope encryption, build/runtime split, rotation.
  Schema change = **new migration file** (SPEC-03 §9), SQL you can defend.
- `lifecycle/` — SPEC-13 deployment state machine, superseding, promotion, rollback
- `k8srender/` — SPEC-14 renderer: pure function, manifest in → object list out,
  byte-identical for identical input (R-9). The AutoPilot three-action
  interface (§5) lives here too — narrow, authenticated, three verbs only.
- `ingress/` — SPEC-15 hosts + wildcard cert flows (cert-manager/Route53)
- `routing/` — SPEC-16 the routing reconciler loop (poll, converge, idempotent)

Hard rules: never `kubectl delete namespace`; never write Ingress outside the
reconciler; promotion = apply, wait Ready, then route row — never reversed
(R-17). Tests: renderer golden-file tests must pass byte-identical; the rest is
unit + contract locally, cluster behaviour verified by the coordinator.

First action in your session: `git merge master` (see `../PARALLEL-PLAN.md`).
