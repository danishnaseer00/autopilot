"""Resolved-manifest input for the build stage (SPEC-06 / SPEC-09).

The deployment row is authoritative when it carries a resolved manifest. A
repository's ``autopilot.yaml`` fills the gap for the first build of a new
project. When neither exists the pipeline synthesises a complete, minimal
manifest from the detected plan; SPEC-06 R-2 forbids a partial manifest at
build time and SPEC-09 R-22 needs a stable ``manifest_hash``.

The synthesis is intentionally conservative: one service, platform-default
resources and port, and a build block that records the detected strategy so a
later build of the same commit resolves identically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from platform_shared.manifest import ManifestDocument, parse_manifest
from platform_shared.manifest.validate import (
    MAX_MANIFEST_BYTES,
    ManifestValidationError,
    ManifestYamlError,
)

from buildplane.detection.detect import ExplicitBuildConfig
from buildplane.detection.plan import BuildPlan, OutputKind
from buildplane.pipeline.errors import ManifestError

MANIFEST_FILENAME = "autopilot.yaml"


def load_manifest(
    root: str | Path, stored: dict[str, Any] | None
) -> tuple[ManifestDocument | None, tuple[str, ...]]:
    """Return the authoritative manifest and any validation warnings.

    ``None`` means the caller must detect first and synthesise one. A malformed
    repository manifest always fails: silently falling back to detection would
    deploy something the customer did not write.
    """

    if stored is not None:
        try:
            return ManifestDocument.model_validate(stored), ()
        except Exception as exc:
            raise ManifestError(
                f"stored deployment manifest is invalid: {exc}",
                code="stored_manifest_invalid",
            ) from exc

    path = Path(root) / MANIFEST_FILENAME
    if not path.exists():
        return None, ()
    if not path.is_file() or path.is_symlink():
        raise ManifestError(
            f"{MANIFEST_FILENAME} must be a regular file inside the repository",
            code="manifest_not_a_file",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ManifestError(
            f"cannot read {MANIFEST_FILENAME}: {exc}", code="manifest_unreadable"
        ) from exc
    if size > MAX_MANIFEST_BYTES:
        raise ManifestError(
            f"{MANIFEST_FILENAME} exceeds the {MAX_MANIFEST_BYTES}-byte limit",
            code="manifest_too_large",
        )
    try:
        text = path.read_text("utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManifestError(
            f"{MANIFEST_FILENAME} is not readable UTF-8: {exc}",
            code="manifest_unreadable",
        ) from exc
    try:
        validated = parse_manifest(text)
    except (ManifestValidationError, ManifestYamlError) as exc:
        raise ManifestError(
            f"{MANIFEST_FILENAME} is invalid: {exc}",
            code="manifest_invalid",
        ) from exc
    return validated.manifest, tuple(validated.warnings)


def select_service(manifest: ManifestDocument, service_name: str | None):
    """Choose the service this build produces.

    A deployment is one image. If the manifest has more than one buildable
    service, the caller must name one; guessing is how the wrong service gets
    deployed. A single buildable service, or a single root-sourced service, is
    deterministic and needs no extra input.
    """

    services = list(manifest.services)
    if not services:
        raise ManifestError("manifest has no services", code="manifest_empty")

    if service_name is not None:
        for service in services:
            if service.name == service_name:
                if service.image is not None and service.build is None:
                    raise ManifestError(
                        f"service {service_name!r} is pulled, not built",
                        code="service_not_buildable",
                    )
                return service
        raise ManifestError(
            f"manifest has no service named {service_name!r}",
            code="service_missing",
        )

    candidates = [
        service
        for service in services
        if service.image is None or service.build is not None
    ]
    if len(candidates) == 1:
        return candidates[0]

    root_candidates = [
        service for service in candidates if normalise_source(service.source) == "."
    ]
    if len(root_candidates) == 1:
        return root_candidates[0]
    raise ManifestError(
        "manifest has multiple buildable services; name one explicitly",
        code="service_required",
    )


def service_root(manifest: ManifestDocument, service_name: str | None) -> str:
    return normalise_source(select_service(manifest, service_name).source)


def normalise_source(source: str) -> str:
    if source in ("", ".", "./"):
        return "."
    raw = source.strip()
    while raw.startswith("./"):
        raw = raw[2:]
    if raw.startswith("/") or "\x00" in raw or "\\" in raw:
        raise ManifestError(
            f"service source is not a relative repository path: {source!r}",
            code="service_source_invalid",
        )
    value = raw.strip("/")
    if not value:
        return "."
    # The index and tar extractor both reject traversal; this is the same check
    # at the manifest boundary so a bad service source fails before detection.
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ManifestError(
            f"service source escapes the repository: {source!r}",
            code="service_source_invalid",
        )
    return value


def synthesise_manifest(
    *,
    name: str,
    plan: BuildPlan,
    source: str,
    explicit: ExplicitBuildConfig | None,
) -> ManifestDocument:
    """Build a complete minimal manifest from a detected plan."""

    service_type = _service_type(plan)
    build: dict[str, Any] | None = None
    if plan.source == "dockerfile":
        build = {
            "strategy": "dockerfile",
            "dockerfile": explicit.dockerfile if explicit and explicit.dockerfile else "Dockerfile",
        }
    elif plan.buildpack is not None:
        build = {"strategy": "buildpack"}
    elif plan.installCommand or plan.buildCommand:
        build = {"strategy": "auto"}
        if explicit is not None and explicit.build_argv is not None:
            build["command"] = list(explicit.build_argv)
    elif plan.outputKind == OutputKind.static:
        build = {"strategy": "none"}

    if build is not None and plan.outputDir is not None:
        build["outputDir"] = plan.outputDir

    document = {
        "version": 1,
        "name": name,
        "services": [
            {
                "name": "web",
                "source": "." if source == "." else source,
                "type": service_type,
                "build": build,
                "runtime": {"port": 3000},
                "resources": {
                    "requests": {"cpu": "100m", "memory": "128Mi"},
                },
            }
        ],
    }
    try:
        return ManifestDocument.model_validate(document)
    except Exception as exc:  # pragma: no cover - defensive: our shape is fixed
        raise ManifestError(
            f"detected plan cannot be represented as a manifest: {exc}",
            code="manifest_synthesis_failed",
        ) from exc


def _service_type(plan: BuildPlan) -> str:
    if plan.outputKind == OutputKind.static:
        return "static"
    if plan.outputKind == OutputKind.container:
        return "stateless"
    if plan.outputKind == OutputKind.functions:
        return "node" if plan.nodeVersion is not None else "python"
    return "stateless"
