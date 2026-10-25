# paas/autopilot — Worker D (SPEC-30 → 35)

The research contribution. **Read `../../CRITIQUE.md` first** — several
requirements exist to prevent specific evaluation failures. Do not start until
the vertical slice runs: fault injection needs real deployments to inject into.

- `inject/` — SPEC-30 Chaos Mesh experiments + Locust load profiles; held-out
  *fault types*, n≥240 injections; namespace scoping only — never platform
  namespaces
- `dataset/` — SPEC-31 window building, features, leakage prevention, splits
- `det/` — SPEC-32 threshold baseline → Isolation Forest → LSTM autoencoder
- `rca/` — SPEC-33 dependency graph from the SPEC-06 manifest + evidence ranking
- `agent/` — SPEC-34 LangGraph agent, RAG, three actions, approval, verification
- `eval/` — SPEC-35 three arms, honest aggregate reporting

Hard rules: rows/features only ever produced through `dataset/`'s versioning
path; model weights are versioned artefacts with a command line to reproduce
them; evaluation runs take the AWS node **exclusively** (PARALLEL-PLAN
hardware rule). Small models, CPU is fine — no GPU needed.

## SPEC-30 status (local core)

Implemented under `src/autopilot/inject/` and covered by deterministic,
cluster-free tests:

- **Catalogue (R-1/R-2):** 10 types. F1–F6 render Chaos Mesh custom resources;
  F7–F10 are application-level hooks, because Chaos Mesh has no leak, pool
  exhaustion, cache-decay or slow-ramp primitive (Q-1/Q-2).
- **Parameters (R-3/R-19):** explicit ranges/steps for every fault, per-run
  seeded sampling, no module-level RNG. F3/F5/F7 have committed within-type
  magnitude holdouts.
- **Partition (R-4–R-7):** `data/heldout-v1.json` pins train
  `F1 F2 F3 F5 F6 F7 F8` and held-out `F4 F9 F10`. A SHA-256 pin makes an
  in-place edit fail closed; a real split change requires a new version and a
  `DECISIONS.md` entry before training.
- **Plan/scale (R-8/R-9):** the default plan is 300 main runs (210 seen-type +
  90 unseen-type, 30 per fault per regime) plus 390 magnitude-holdout runs =
  690 total. Plans below 30/type/regime are rejected.
- **Artifacts (R-10):** Chaos Mesh YAML, `ApplicationFaultRequest` JSON, Locust
  load profile + locustfile, and an ordered runbook. Every artifact is scoped
  to exactly one `autopilot-test-*` namespace; platform namespaces and
  `namespaceSelector` are rejected.
- **Ground truth (R-11–R-13):** written at injection time as write-once
  `runs/<run_id>/ground-truth.json`; recovery and discard are separate
  write-once files merged by `GroundTruthStore.load()` for SPEC-31. No
  telemetry-derived truth and no retro-fitted recovery.
- **Failure guards (§9):** dirty app, load not reaching the app, unobservable
  injection, telemetry gap, recovery timeout discard; controller loss halts the
  harness; three unobservable runs force a fault redesign.
- **Test apps (R-14–R-16):** fixed pinned catalogue (`storefront`: frontend →
  backend → database) with metrics/logs/traces on by default and no app-side
  telemetry configuration.

### Commands

```powershell
cd paas/autopilot
$env:PYTHONPATH = "src"
python -m pytest
python -m autopilot.inject.cli verify
python -m autopilot.inject.cli accounts
python -m autopilot.inject.cli plan --out "$env:TEMP\spec30-plan" --artifacts-limit 5
```

`plan` writes the complete 690-run plan and renders artifacts only for the
first `--artifacts-limit` runs; the plan digest and counts are unchanged.

### Deferred until the vertical slice / AWS node

There is **no cluster adapter yet**, so these are not claimed as passing:
applying Chaos Mesh resources or application hooks to a live deployment,
starting/observing Locust, scraping telemetry, detecting an unobservable
injection or telemetry gap in live data, polling recovery, archiving telemetry,
and the live legs of AC-4/AC-6/AC-7/AC-11. The local tests prove the state
machines and artifact contracts those steps must use. SPEC-31 is intentionally
not started pending coordinator review.
