"""SPEC-13 deployment lifecycle.

The state machine is pure (``state``); the service is transactional and takes a
caller-owned connection (``service``); the queue boundary is a Protocol so
SPEC-11 can supply the real claim/lease implementation.
"""

from runtime.lifecycle.errors import (
    AuditUnavailable,
    DeploymentNotFound,
    DeploymentNotReady,
    EnqueueFailed,
    InvalidDeployment,
    InvalidTransition,
    LifecycleError,
    NoRoute,
    ProjectNotFound,
    RollbackNotAvailable,
)
from runtime.lifecycle.models import (
    Deployment,
    PromotionResult,
    TriggerResult,
    deployment_from_row,
)
from runtime.lifecycle.queue import BuildQueue, PostgresBuildQueue
from runtime.lifecycle.service import DeploymentLifecycle
from runtime.lifecycle.state import (
    ACTIVE_STATUSES,
    DEFAULT_DRAIN_SECONDS,
    DrainPolicy,
    STATUSES,
    TERMINAL_STATUSES,
    TRANSITIONS,
    assert_transition,
    canonical_manifest_hash,
    idempotency_key,
    is_active,
    is_terminal,
    promotion_lock_key,
)

__all__ = [
    "ACTIVE_STATUSES",
    "AuditUnavailable",
    "BuildQueue",
    "DEFAULT_DRAIN_SECONDS",
    "Deployment",
    "DeploymentLifecycle",
    "DeploymentNotFound",
    "DeploymentNotReady",
    "DrainPolicy",
    "EnqueueFailed",
    "InvalidDeployment",
    "InvalidTransition",
    "LifecycleError",
    "NoRoute",
    "PostgresBuildQueue",
    "ProjectNotFound",
    "PromotionResult",
    "RollbackNotAvailable",
    "STATUSES",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "TriggerResult",
    "assert_transition",
    "canonical_manifest_hash",
    "deployment_from_row",
    "idempotency_key",
    "is_active",
    "is_terminal",
    "promotion_lock_key",
]
