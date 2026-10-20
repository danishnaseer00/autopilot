"""SPEC-14 error types.

The renderer is pure: every failure here is a caller error (bad manifest, bad
context) or an explicit refusal, never a cluster observation. Cluster errors are
raised by the adapter and carry no object bodies - a body can contain a Secret.
"""

from __future__ import annotations


class RenderError(Exception):
    """The manifest or render context cannot produce a safe object set."""


class InvalidRenderContext(RenderError):
    """A namespace, UUID, revision or replica count is not usable."""


class ClusterError(Exception):
    """The Kubernetes API rejected an operation."""


class RolloutTimeout(ClusterError):
    """The workload did not become ready inside the SPEC-14 R-17 timeout."""


class RolloutFailed(ClusterError):
    """Apply succeeded but readiness failed; the route was not promoted."""


class ActionError(Exception):
    """Base class for the narrow SPEC-14 R-20 action interface."""


class AuthenticationFailed(ActionError):
    """The internal agent credential is missing or wrong."""


class InvalidAction(ActionError):
    """The request is outside the three actions or carries extra fields."""


class ApprovalRequired(ActionError):
    """A scale action has no valid, unexpired, exactly-bound approval."""


class ScaleLimitExceeded(ActionError):
    """The requested replica count exceeds the manifest/org ceiling."""


__all__ = [
    "ActionError",
    "ApprovalRequired",
    "AuthenticationFailed",
    "ClusterError",
    "InvalidAction",
    "InvalidRenderContext",
    "RenderError",
    "RolloutFailed",
    "RolloutTimeout",
    "ScaleLimitExceeded",
]
