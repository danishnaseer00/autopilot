"""Error taxonomy for SPEC-13.

None of these messages may carry a secret value or a manifest/env value. Ids,
refs, SHAs and state names are safe; callers may log them.
"""

from __future__ import annotations

import uuid


class LifecycleError(Exception):
    """Base class for every deployment-lifecycle failure."""


class InvalidDeployment(LifecycleError, ValueError):
    """Input is not a valid deployment fact (SHA, ref, image, manifest, ...)."""


class ProjectNotFound(LifecycleError):
    """The project is absent, or outside the caller's tenant (RLS + predicate)."""


class DeploymentNotFound(LifecycleError):
    """No deployment row matches the id within the caller's tenant."""


class InvalidTransition(LifecycleError):
    """The requested state move is not allowed by the SPEC-13 state table."""

    def __init__(
        self,
        deployment_id: uuid.UUID,
        current: str,
        target: str,
    ) -> None:
        super().__init__(
            f"deployment {deployment_id} cannot move from {current!r} to {target!r}"
        )
        self.deployment_id = deployment_id
        self.current = current
        self.target = target


class DeploymentNotReady(LifecycleError):
    """Promotion requires a built, verified revision in ``ready`` (R-1/R-4)."""


class RollbackNotAvailable(LifecycleError):
    """The target has no retained revision, so rollback would need a rebuild."""


class NoRoute(LifecycleError):
    """No route row is available to cut over; routing is a SPEC-15/16 concern."""


class EnqueueFailed(LifecycleError):
    """The build job could not be enqueued, so the trigger must roll back."""


class AuditUnavailable(LifecycleError):
    """The advisory audit row could not be written; the action is refused."""


__all__ = [
    "AuditUnavailable",
    "DeploymentNotFound",
    "DeploymentNotReady",
    "EnqueueFailed",
    "InvalidDeployment",
    "InvalidTransition",
    "LifecycleError",
    "NoRoute",
    "ProjectNotFound",
    "RollbackNotAvailable",
]
