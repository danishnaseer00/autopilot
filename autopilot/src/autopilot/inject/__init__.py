"""SPEC-30 fault injection harness."""

from autopilot.inject.catalogue import (
    FAULT_CATALOGUE,
    FAULT_IDS,
    FaultSpec,
    get_fault,
    validate_catalogue,
)
from autopilot.inject.errors import (
    ArtifactScopeError,
    CatalogueError,
    ChaosRenderError,
    GroundTruthError,
    HarnessHalt,
    ParameterError,
    PartitionError,
    PlanValidationError,
    RunGuardError,
    TestAppCatalogueError,
)

__all__ = [
    "ArtifactScopeError",
    "CatalogueError",
    "ChaosRenderError",
    "FAULT_CATALOGUE",
    "FAULT_IDS",
    "FaultSpec",
    "GroundTruthError",
    "HarnessHalt",
    "ParameterError",
    "PartitionError",
    "PlanValidationError",
    "RunGuardError",
    "TestAppCatalogueError",
    "get_fault",
    "validate_catalogue",
]
