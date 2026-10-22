"""SPEC-11 build queue and fairness.

Claim/lease primitives live in ``jobs``; admission/backpressure in ``admission``;
retry/DLQ decisions in ``retry``; the queue-depth autoscaler policy in
``autoscaling``; and the orphaned-deployment reconciler in ``reconcile``.
"""

from buildplane.queue.admission import (
    DEFAULT_MAX_QUEUE_DEPTH,
    BuildEnqueueRateLimiter,
    InMemoryEnqueueRateLimiter,
    PostgresQueueAdmission,
    QueueAdmissionPolicy,
)
from buildplane.queue.autoscaling import AutoscaleDecision, desired_workers
from buildplane.queue.errors import (
    QuotaExceeded,
    QueueBackpressure,
    QueueError,
    QueueFull,
    QueueUnavailable,
)
from buildplane.queue.models import (
    AdmissionDecision,
    DeadLetter,
    ReconciledDeployment,
)
from buildplane.queue.reconcile import PostgresQueueReconciler
from buildplane.queue.retry import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_BACKOFF_SECONDS,
    RetryDecision,
    backoff_seconds,
    decide_retry,
    stable_jitter,
)

# ``jobs`` imports buildplane.pipeline.models. Keep it lazy so importing
# buildplane.queue from the webhook store cannot re-enter the pipeline package
# while that package is still initialising.
_LAZY_EXPORTS = {
    "DEFAULT_ORG_CONCURRENCY": ("buildplane.queue.jobs", "DEFAULT_ORG_CONCURRENCY"),
    "HEARTBEAT_SECONDS": ("buildplane.queue.jobs", "HEARTBEAT_SECONDS"),
    "LEASE_SECONDS": ("buildplane.queue.jobs", "LEASE_SECONDS"),
    "PostgresJobQueue": ("buildplane.queue.jobs", "PostgresJobQueue"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


__all__ = [
    "AdmissionDecision",
    "AutoscaleDecision",
    "BuildEnqueueRateLimiter",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_QUEUE_DEPTH",
    "DEFAULT_ORG_CONCURRENCY",
    "DeadLetter",
    "HEARTBEAT_SECONDS",
    "InMemoryEnqueueRateLimiter",
    "LEASE_SECONDS",
    "MAX_BACKOFF_SECONDS",
    "PostgresJobQueue",
    "PostgresQueueAdmission",
    "PostgresQueueReconciler",
    "QuotaExceeded",
    "QueueAdmissionPolicy",
    "QueueBackpressure",
    "QueueError",
    "QueueFull",
    "QueueUnavailable",
    "ReconciledDeployment",
    "RetryDecision",
    "backoff_seconds",
    "decide_retry",
    "desired_workers",
    "stable_jitter",
]
