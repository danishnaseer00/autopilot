"""Named errors for the SPEC-30 harness.

Every failure that would make an evaluation result uninterpretable is a named,
fail-closed error rather than a fallback.  The dataset is the deliverable; a
silently repaired experiment is worse than a stopped one.
"""

from __future__ import annotations


class FaultInjectionError(Exception):
    """Base class for SPEC-30 harness failures."""


class CatalogueError(FaultInjectionError):
    """The fault catalogue does not satisfy SPEC-30 R-1/R-2."""


class ParameterError(FaultInjectionError):
    """A fault parameter is missing, out of range or inconsistent."""


class PartitionError(FaultInjectionError):
    """The committed held-out partition is invalid or was edited in place."""


class PlanValidationError(FaultInjectionError):
    """An experiment plan is invalid and must not be executed."""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        detail = "; ".join(self.errors) if self.errors else "invalid experiment plan"
        super().__init__(detail)


class TestAppCatalogueError(FaultInjectionError):
    """The fixed test-application catalogue is invalid or was edited."""


class ChaosRenderError(FaultInjectionError):
    """A Chaos Mesh manifest could not be rendered safely."""


class ArtifactScopeError(FaultInjectionError):
    """An artifact names a namespace that is not a dedicated test namespace."""


class GroundTruthError(FaultInjectionError):
    """Ground truth is missing, malformed or was written twice."""


class RunGuardError(FaultInjectionError):
    """A run attempted an illegal lifecycle transition."""


class HarnessHalt(FaultInjectionError):
    """A S1/S3 condition means the whole harness must stop.

    SPEC-30 §9: a partial injection or a dirty application is never allowed to
    continue into the next run.
    """
