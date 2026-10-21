"""SPEC-09 build pipeline."""

from buildplane.pipeline.builder_images import BuilderImageCatalog
from buildplane.pipeline.cache import (
    BuildCache,
    CacheAudit,
    CacheEntry,
    InMemoryBuildCache,
    build_cache_key,
    dependency_cache_key,
    hash_output_tree,
    image_digest,
    verify_cache_entry,
)
from buildplane.pipeline.errors import (
    CacheError,
    ExtractionError,
    FetchError,
    LeaseLost,
    LogStoreError,
    ManifestError,
    PipelineError,
    SandboxFailure,
    SandboxTimeout,
    SecretRedactionUnavailable,
    SecretResolutionError,
    StrategyError,
)
from buildplane.pipeline.fetch import (
    FetchedRepository,
    GitHubTarballFetcher,
    StaticTarballFetcher,
    TarballFetcher,
    safe_extract_tarball,
)
from buildplane.pipeline.logs import (
    InMemoryLogStore,
    LocalLogStore,
    LogSink,
    LogStore,
    LogStream,
    validate_redaction_values,
)
from buildplane.pipeline.models import (
    Artifact,
    BuildInputs,
    BuildOutcome,
    CacheOutcome,
    ClaimedJob,
    Redactor,
    SandboxResult,
    SecretProvider,
    Stage,
)
from buildplane.pipeline.queue import PostgresJobQueue
from buildplane.pipeline.reporting import (
    BuildReporter,
    InMemoryBuildReporter,
    PostgresBuildReporter,
    backoff_seconds,
)
from buildplane.pipeline.sandbox import (
    DEFAULT_KILL_GRACE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    REGISTRY_ONLY_NETWORK,
    DockerSandboxRunner,
    SandboxConfig,
    SandboxResources,
    build_sandbox_config,
    new_container_name,
    wait_with_watchdog,
)

# ``strategy`` imports the supply-chain package, and ``service`` imports both.
# Keep them lazy so ``buildplane.pipeline.errors`` (used by supply-chain errors)
# can be imported without initialising the whole pipeline and creating a cycle.
_LAZY_EXPORTS = {
    "BuildPipeline": ("buildplane.pipeline.service", "BuildPipeline"),
    "ResolvedStrategy": ("buildplane.pipeline.strategy", "ResolvedStrategy"),
    "lockfile_hash": ("buildplane.pipeline.strategy", "lockfile_hash"),
    "resolve_for_manifest": ("buildplane.pipeline.strategy", "resolve_for_manifest"),
    "resolve_strategy": ("buildplane.pipeline.strategy", "resolve_strategy"),
    "strategy_id": ("buildplane.pipeline.strategy", "strategy_id"),
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
    "Artifact",
    "BuildCache",
    "BuildInputs",
    "BuildOutcome",
    "BuildPipeline",
    "BuildReporter",
    "BuilderImageCatalog",
    "CacheAudit",
    "CacheEntry",
    "CacheError",
    "CacheOutcome",
    "ClaimedJob",
    "DEFAULT_KILL_GRACE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DockerSandboxRunner",
    "ExtractionError",
    "FetchError",
    "FetchedRepository",
    "GitHubTarballFetcher",
    "InMemoryBuildCache",
    "InMemoryBuildReporter",
    "InMemoryLogStore",
    "LeaseLost",
    "LocalLogStore",
    "LogSink",
    "LogStore",
    "LogStoreError",
    "LogStream",
    "ManifestError",
    "PipelineError",
    "PostgresBuildReporter",
    "PostgresJobQueue",
    "Redactor",
    "REGISTRY_ONLY_NETWORK",
    "ResolvedStrategy",
    "SandboxConfig",
    "SandboxFailure",
    "SandboxResources",
    "SandboxResult",
    "SandboxTimeout",
    "SecretProvider",
    "SecretRedactionUnavailable",
    "SecretResolutionError",
    "Stage",
    "StaticTarballFetcher",
    "StrategyError",
    "TarballFetcher",
    "backoff_seconds",
    "build_cache_key",
    "build_sandbox_config",
    "dependency_cache_key",
    "hash_output_tree",
    "image_digest",
    "lockfile_hash",
    "new_container_name",
    "resolve_for_manifest",
    "resolve_strategy",
    "safe_extract_tarball",
    "strategy_id",
    "validate_redaction_values",
    "verify_cache_entry",
    "wait_with_watchdog",
]
