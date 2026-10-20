"""The AutoPilot build plane (SPEC-07..12).

`detection` is SPEC-07; `gitware` is the SPEC-08 webhook receiver and repo
ownership verification. The pipeline, queue, supply-chain and registry modules
land in their own spec order; see `paas/build/README.md`.
"""

__all__ = ["detection", "gitware"]
