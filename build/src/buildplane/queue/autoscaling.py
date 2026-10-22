"""Queue-depth autoscaler policy (SPEC-11 R-13..R-15).

This is the cost policy, not the Kubernetes integration. The coordinator owns
applying the returned worker count to the cluster (HPA/Deployment/DECISIONS
gates); the policy is pure so the ceiling and alert trigger are testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AutoscaleDecision:
    """The next worker count and the cost-ceiling signal."""

    desired_workers: int
    raw_workers: float
    queue_depth: int
    at_ceiling: bool
    alert: str | None


def desired_workers(
    *,
    queue_depth: int,
    avg_build_seconds: float,
    safety_factor: float,
    min_workers: int,
    max_workers: int,
) -> AutoscaleDecision:
    """Scale on queue depth, clamped by the warm floor and budget ceiling."""

    if queue_depth < 0:
        raise ValueError("queue_depth must not be negative")
    if avg_build_seconds <= 0:
        raise ValueError("avg_build_seconds must be positive")
    if safety_factor <= 0:
        raise ValueError("safety_factor must be positive")
    if min_workers < 0:
        raise ValueError("min_workers must not be negative")
    if max_workers <= 0:
        raise ValueError("max_workers must be positive")
    if min_workers > max_workers:
        raise ValueError("min_workers must not exceed max_workers")

    raw = (queue_depth / avg_build_seconds) * safety_factor
    desired = max(min_workers, math.ceil(raw))
    at_ceiling = queue_depth > 0 and desired >= max_workers
    if queue_depth > 0 and raw > max_workers:
        desired = max_workers
    elif desired > max_workers:
        desired = max_workers

    alert = None
    if at_ceiling:
        alert = (
            "build autoscaler at BUDGET_MAX_WORKERS="
            f"{max_workers} (queue_depth={queue_depth})"
        )
    return AutoscaleDecision(
        desired_workers=desired,
        raw_workers=raw,
        queue_depth=queue_depth,
        at_ceiling=at_ceiling,
        alert=alert,
    )
