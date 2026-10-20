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
