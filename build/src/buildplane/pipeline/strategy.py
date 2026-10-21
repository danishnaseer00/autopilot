"""Build-strategy resolution and cache inputs (SPEC-09 §3.4, R-22..R-26).

This module is the seam between SPEC-07's pure detection result and the
sandbox. It preserves the manifest's explicit build argv, chooses the lockfile
whose bytes key the dependency layer, and derives the stable ``strategy_id``
used by the build cache.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from buildplane.detection.detect import (
    DetectionResult,
    ExplicitBuildConfig,
    detect_plan,
    explicit_from_service,
)
from buildplane.detection.index import DetectionError, RepositoryIndex
from buildplane.detection.plan import BuildPlan, PackageManager
from buildplane.pipeline.errors import StrategyError
from buildplane.pipeline.manifest_resolution import ManifestDocument, select_service

LOCKFILES: dict[PackageManager, str] = {
    PackageManager.npm: "package-lock.json",
    PackageManager.pnpm: "pnpm-lock.yaml",
    PackageManager.yarn: "yarn.lock",
    PackageManager.bun: "bun.lockb",
}

_EMPTY_LOCKFILE_HASH = "sha256:" + hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True, slots=True)
class ResolvedStrategy:
    detection: DetectionResult
    service_name: str
    source: str
    explicit: ExplicitBuildConfig | None
    explicit_argv: tuple[str, ...] | None
    dockerfile: str | None
    strategy_id: str
    lockfile_hash: str
    output_dir: str | None

    @property
    def plan(self) -> BuildPlan:
        return self.detection.plan


def resolve_for_manifest(
    index: RepositoryIndex,
    manifest: ManifestDocument | None,
    *,
    service_name: str | None = None,
    read_file: Callable[[str], bytes | None] | None = None,
) -> ResolvedStrategy:
    """Resolve detection and lockfile inputs for one manifest service."""

    if manifest is None:
        service = None
        source = "."
        explicit = None
    else:
        service = select_service(manifest, service_name)
        source = _source_for(service)
        explicit = explicit_from_service(service)

    return resolve_strategy(
        index,
        explicit,
        service_name=service.name if service is not None else "web",
        source=source,
        explicit_argv=explicit.build_argv if explicit is not None else None,
        dockerfile=explicit.dockerfile if explicit is not None else None,
        read_file=read_file,
    )


def resolve_strategy(
    index: RepositoryIndex,
    explicit: ExplicitBuildConfig | None,
    *,
    service_name: str = "web",
    source: str = ".",
    explicit_argv: tuple[str, ...] | None = None,
    dockerfile: str | None = None,
    read_file: Callable[[str], bytes | None] | None = None,
) -> ResolvedStrategy:
    try:
        detection = detect_plan(index, explicit)
    except DetectionError as exc:
        raise StrategyError(
            exc.message,
            code=exc.code,
            detected=exc.detected,
        ) from exc

    plan = detection.plan
    if explicit_argv is None and explicit is not None:
        explicit_argv = explicit.build_argv
    if dockerfile is None and plan.source == "dockerfile":
        dockerfile = explicit.dockerfile if explicit and explicit.dockerfile else "Dockerfile"

    return ResolvedStrategy(
        detection=detection,
        service_name=service_name,
        source=source,
        explicit=explicit,
        explicit_argv=explicit_argv,
        dockerfile=dockerfile,
        strategy_id=strategy_id(plan),
        lockfile_hash=lockfile_hash(plan, read_file),
        output_dir=plan.outputDir,
    )


def _source_for(service) -> str:
    value = service.source
    if value in ("", ".", "./"):
        return "."
    return value.strip().strip("/")


def strategy_id(plan: BuildPlan) -> str:
    """Stable strategy component of the cache key (R-25).

    Node version is encoded here rather than added as a seventh cache-key field:
    R-25 names five components, while R-22 and SPEC-19 require the Node major to
    be an input. Folding it into the strategy id satisfies both without silently
    changing the published formula.
    """

    parts = [str(plan.source), str(plan.outputKind)]
    if plan.buildpack:
        parts.append(plan.buildpack)
    if plan.nodeVersion is not None:
        parts.append(f"node{plan.nodeVersion}")
    return ":".join(parts)


def lockfile_hash(
    plan: BuildPlan,
    read_file: Callable[[str], bytes | None] | None,
) -> str:
    if plan.packageManager is None or read_file is None:
        return _EMPTY_LOCKFILE_HASH
    manager = plan.packageManager
    if not isinstance(manager, PackageManager):
        manager = PackageManager(manager)
    name = LOCKFILES[manager]
    content = read_file(name)
    if content is None:
        return _EMPTY_LOCKFILE_HASH
    return "sha256:" + hashlib.sha256(content).hexdigest()
