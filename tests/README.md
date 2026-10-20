# paas/tests — the SPEC-38 harness home (Coordinator runs the gates)

```
unit/        — refs the per-module tests living next to the code (convention)
contract/    — SPEC-36 console contract, SPEC-06 manifest contract, DB shape
isolation/   — SPEC-38 §3, the 13 tenant-isolation tests (kind/k3s - CI)
security/    — SPEC-38 §5, the 12 security checks — the gate that blocks merge
fixtures/    — dockerised Postgres/Seed data per SPEC-38 R-15, shared
```

Who runs what:

| Suite | Who | Where |
|---|---|---|
| unit + contract | runtime worker session | locally (containerised PG/Redis) |
| isolation + security | coordinator only | kind in CI / k3s AWS node, on the *validated* branch of the approved repository |

A gate that fails sends the work back with the failing criterion quoted —
"That is not done, it is not merged." The definition of done is the spec's
acceptance criteria, nothing softer.
