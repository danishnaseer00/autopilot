"""Framework detection and build-plan resolution (SPEC-07)."""

from buildplane.detection.cache import (
    DEFAULT_TTL_SECONDS,
    InMemoryPlanCache,
    PlanCache,
    RedisPlanCache,
    detect_with_cache,
    detection_cache_key,
)
from buildplane.detection.detect import (
    DetectionResult,
    ExplicitBuildConfig,
    detect_plan,
    explicit_digest,
    explicit_from_service,
)
from buildplane.detection.index import (
    METADATA_FILES,
    DetectionError,
    RepositoryIndex,
    normalise_repo_path,
)
from buildplane.detection.plan import (
    NULL_MEANINGS,
    BuildPlan,
    OutputKind,
    PackageManager,
    PlanSource,
    compute_detection_hash,
)
from buildplane.detection.sourcemaps import (
    MapReference,
    RewriteResult,
    rewrite_source_maps,
    tenant_map_prefix,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "METADATA_FILES",
    "NULL_MEANINGS",
    "BuildPlan",
    "DetectionError",
    "DetectionResult",
    "ExplicitBuildConfig",
    "InMemoryPlanCache",
    "MapReference",
    "OutputKind",
    "PackageManager",
    "PlanCache",
    "PlanSource",
    "RedisPlanCache",
    "RepositoryIndex",
    "RewriteResult",
    "compute_detection_hash",
    "detect_plan",
    "detect_with_cache",
    "detection_cache_key",
    "explicit_digest",
    "explicit_from_service",
    "normalise_repo_path",
    "rewrite_source_maps",
    "tenant_map_prefix",
]
