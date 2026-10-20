"""The resolver: tree + explicit config -> BuildPlan (SPEC-07 §4).

Resolution order (first match wins, and explicit config always wins):

    explicit configuration / manifest
      > Dockerfile at the source root
        > framework dependency (R-6 table)
          > index.html (static shape)
            > package.json + start script (Node service)
              > named failure (R-10: never guess a build command)

The numbering in §4 puts the Dockerfile stage after framework detection but its
text (and AC-5) says a Dockerfile *wins over* framework detection; this module
implements the text, which is the only reading that satisfies AC-5.

This function is pure (R-1). It reads an immutable `RepositoryIndex`, never the
clock, network, environment or filesystem, and it executes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # platform-shared is a hard dependency; keep import-time light
    from platform_shared.manifest.models import ServiceEntry

from buildplane.detection.frameworks import (
    BUILDPACK_NODE,
    BUILDPACK_PYTHON,
    DEFAULT_NODE_VERSION,
    NODE_VERSION_FILES,
    PYTHON_SIGNALS,
    build_command,
    detect_package_manager,
    find_framework,
    install_command,
)
from buildplane.detection.index import DetectionError, RepositoryIndex, normalise_repo_path
from buildplane.detection.plan import (
    BuildPlan,
    OutputKind,
    PackageManager,
    PlanSource,
    compute_detection_hash,
)

BuildStrategy = Literal["auto", "dockerfile", "buildpack", "none"]
_STRATEGIES = frozenset({"auto", "dockerfile", "buildpack", "none"})
_NODE_MAJOR_RE = re.compile(r"(?<!\d)(\d{1,2})(?!\d)")


@dataclass(frozen=True)
class ExplicitBuildConfig:
    """The explicit layer (SPEC-06 manifest / project settings).

    SPEC-06's `BuildBlock` currently carries only `strategy` and `dockerfile`;
    SPEC-07 R-3 and AC-4 also speak of an explicit `build.command`. That gap is
    logged as a spec-change proposal in DECISIONS.md. Until SPEC-06 grows the
    keys, the control plane passes the equivalent values here - never by editing
    the shared manifest package (SPEC-06 R-15).

    Commands are data. This module never executes them, and SPEC-09 must pass
    them to the build container as separate argv entries (SPEC-02 R-1).
    """

    strategy: BuildStrategy | None = None
    dockerfile: str | None = None
    package_manager: PackageManager | None = None
    install_command: str | None = None
    build_command: str | None = None
    output_dir: str | None = None
    output_kind: OutputKind | None = None
    node_version: int | None = None
    buildpack: str | None = None

    def __post_init__(self) -> None:
        if self.strategy is not None and self.strategy not in _STRATEGIES:
            raise DetectionError(
                "invalid_explicit_config",
                f"unknown build strategy {self.strategy!r}; expected one of {sorted(_STRATEGIES)}",
            )
        if self.package_manager is not None and not isinstance(
            self.package_manager, PackageManager
        ):
            object.__setattr__(
                self, "package_manager", PackageManager(self.package_manager)
            )
        if self.output_kind is not None and not isinstance(self.output_kind, OutputKind):
            object.__setattr__(self, "output_kind", OutputKind(self.output_kind))
        if self.node_version is not None and not (1 <= self.node_version <= 99):
            raise DetectionError(
                "invalid_explicit_config",
                f"node_version must be a major version, got {self.node_version}",
            )
        if self.output_dir is not None:
            _check_relative_dir(self.output_dir, "output_dir")
        if self.dockerfile is not None:
            _check_relative_file(self.dockerfile, "dockerfile")
        if self.strategy == "none" and self.dockerfile is not None:
            raise DetectionError(
                "invalid_explicit_config",
                "strategy 'none' cannot also name a Dockerfile",
            )
        for field_name, value in (
            ("install_command", self.install_command),
            ("build_command", self.build_command),
        ):
            if value is not None and "\x00" in value:
                raise DetectionError(
                    "invalid_explicit_config", f"{field_name} contains a NUL byte"
                )


@dataclass(frozen=True)
class DetectionResult:
    """The plan plus non-fatal findings (e.g. two lockfiles, R-4/§8)."""

    plan: BuildPlan
    warnings: tuple[str, ...] = ()

    def to_cache_json(self) -> str:
        return json.dumps(
            {"plan": self.plan.to_wire(), "warnings": list(self.warnings)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @classmethod
    def from_cache_json(cls, text: str) -> "DetectionResult":
        payload = json.loads(text)
        return cls(
            plan=BuildPlan.model_validate(payload["plan"]),
            warnings=tuple(str(w) for w in payload.get("warnings", [])),
        )


def explicit_from_service(service: "ServiceEntry") -> ExplicitBuildConfig | None:
    """Map a resolved manifest service's `build` block onto the explicit layer.

    SPEC-06 `BuildBlock` carries `strategy` and `dockerfile` today. When it grows
    `command`/`outputDir` (proposal logged in DECISIONS.md), this is the single
    function that changes - detection never re-implements the manifest model
    (SPEC-06 R-15).
    """
    build = service.build
    if build is None:
        return None
    return ExplicitBuildConfig(strategy=build.strategy, dockerfile=build.dockerfile)


def detect_plan(
    index: RepositoryIndex,
    explicit: ExplicitBuildConfig | None = None,
) -> DetectionResult:
    """Resolve one build plan, or raise a named `DetectionError` (S3)."""
    warnings: list[str] = []
    package = _load_package_json(index)
    dependencies = _dependencies(package)

    manager, lockfiles, manager_warnings = detect_package_manager(index.has)
    warnings.extend(manager_warnings)
    node_project = package is not None or bool(lockfiles)
    if not node_project:
        manager = None  # type: ignore[assignment]  # contract: null = not a JS project

    framework = find_framework(dependencies)
    python_project = any(index.has(signal) for signal in PYTHON_SIGNALS)
    dockerfile = _resolve_dockerfile(index, explicit)

    if explicit is not None and explicit.strategy == "none":
        values = _base_values(OutputKind.none, PlanSource.explicit)
    elif dockerfile is not None:
        values = _base_values(OutputKind.container, PlanSource.dockerfile)
    elif explicit is not None and explicit.strategy == "buildpack":
        values = _buildpack_values(index, package, manager, framework, python_project)
        values["source"] = PlanSource.explicit
    elif framework is not None:
        values = _framework_values(index, framework, manager, package)
    elif python_project:
        values = _python_values()
    elif index.has("index.html"):
        values = _static_values()
    elif _has_start_script(package):
        values = _node_fallback_values(index, manager, package)
    else:
        raise _unrecognised(index)

    if explicit is not None:
        _apply_explicit(values, explicit)

    base = BuildPlan(
        **values,
        detectionHash=compute_detection_hash(
            index.detection_inputs(),
            index.read("package.json") if index.has_metadata("package.json") else None,
            index.read("Dockerfile") if index.has_metadata("Dockerfile") else None,
        ),
    )
    return DetectionResult(plan=base, warnings=tuple(warnings))


# --------------------------------------------------------------------------
# base plans
# --------------------------------------------------------------------------


def _base_values(kind: OutputKind, source: PlanSource) -> dict[str, Any]:
    return {
        "packageManager": None,
        "installCommand": None,
        "buildCommand": None,
        "outputDir": None,
        "outputKind": kind,
        "nodeVersion": None,
        "buildpack": None,
        "source": source,
    }


def _framework_values(
    index: RepositoryIndex,
    rule,
    manager: PackageManager | None,
    package: dict[str, Any] | None,
) -> dict[str, Any]:
    node_version = _resolve_node_version(index, package)
    if rule.buildpack is not None:
        # The buildpack performs install and build; the pipeline runs `pack`.
        return {
            "packageManager": manager,
            "installCommand": None,
            "buildCommand": None,
            "outputDir": None,
            "outputKind": rule.output_kind,
            "nodeVersion": node_version,
            "buildpack": rule.buildpack,
            "source": PlanSource.framework,
        }

    values = _base_values(rule.output_kind, PlanSource.framework)
    if manager is None:  # unreachable: a framework dependency implies package.json
        raise RuntimeError("framework detection resolved without a package manager")
    values.update(
        packageManager=manager,
        installCommand=install_command(manager, index.has),
        buildCommand=build_command(manager, rule.build_script),
        outputDir=rule.output_dir,
        nodeVersion=node_version,
    )
    return values


def _buildpack_values(
    index: RepositoryIndex,
    package: dict[str, Any] | None,
    manager: PackageManager | None,
    framework,
    python_project: bool,
) -> dict[str, Any]:
    buildpack = framework.buildpack if framework is not None else None
    if buildpack is None and python_project:
        buildpack = BUILDPACK_PYTHON
    if buildpack is None and package is not None:
        buildpack = BUILDPACK_NODE
    if buildpack is None:
        raise DetectionError(
            "unrecognised_project",
            "build strategy is 'buildpack' but nothing identified a buildpack: "
            "no supported framework dependency, no requirements.txt/pyproject.toml, "
            "and no package.json",
            detected=tuple(index.paths),
        )

    is_node = buildpack != BUILDPACK_PYTHON and package is not None
    values = _base_values(OutputKind.functions, PlanSource.explicit)
    values.update(
        packageManager=manager if is_node else None,
        nodeVersion=_resolve_node_version(index, package) if is_node else None,
        buildpack=buildpack,
    )
    return values


def _static_values() -> dict[str, Any]:
    values = _base_values(OutputKind.static, PlanSource.fallback)
    # SPEC-07 R-8: serve the source directory itself.
    values["outputDir"] = "."
    return values


def _python_values() -> dict[str, Any]:
    # R-6: requirements.txt / pyproject.toml -> heroku/python, functions.
    values = _base_values(OutputKind.functions, PlanSource.framework)
    values["buildpack"] = BUILDPACK_PYTHON
    return values


def _node_fallback_values(
    index: RepositoryIndex,
    manager: PackageManager,
    package: dict[str, Any],
) -> dict[str, Any]:
    values = _base_values(OutputKind.functions, PlanSource.fallback)
    values.update(
        packageManager=manager,
        nodeVersion=_resolve_node_version(index, package),
        buildpack=BUILDPACK_NODE,
    )
    return values


# --------------------------------------------------------------------------
# explicit overrides
# --------------------------------------------------------------------------


def _apply_explicit(
    values: dict[str, Any],
    explicit: ExplicitBuildConfig,
) -> None:
    """Overlay explicit configuration (R-3/AC-4).

    An explicit `strategy` already chose the base plan; field-level values win
    over anything detection decided. `source` becomes "explicit" unless the
    winning route was a Dockerfile, because that is the more specific claim.
    """
    mapping = (
        ("packageManager", explicit.package_manager),
        ("installCommand", explicit.install_command),
        ("buildCommand", explicit.build_command),
        ("outputDir", _normalise_output_dir(explicit.output_dir)),
        ("outputKind", explicit.output_kind),
        ("nodeVersion", explicit.node_version),
        ("buildpack", explicit.buildpack),
    )
    touched = False
    for key, value in mapping:
        if value is not None:
            values[key] = value
            touched = True

    if explicit.strategy == "dockerfile":
        values["source"] = PlanSource.dockerfile
    elif touched or explicit.strategy in ("none", "buildpack"):
        values["source"] = PlanSource.explicit


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _resolve_dockerfile(
    index: RepositoryIndex, explicit: ExplicitBuildConfig | None
) -> str | None:
    if explicit is not None and explicit.dockerfile is not None:
        path = explicit.dockerfile
        if not index.has(path):
            raise DetectionError(
                "dockerfile_missing",
                f"explicit Dockerfile {path!r} does not exist in the source tree",
                path=path,
            )
        return path
    if explicit is not None and explicit.strategy == "dockerfile":
        if not index.has("Dockerfile"):
            raise DetectionError(
                "dockerfile_missing",
                "build strategy is 'dockerfile' but no Dockerfile exists at the source root",
                path="Dockerfile",
            )
        return "Dockerfile"
    if explicit is not None and explicit.strategy in ("none", "buildpack"):
        return None
    return "Dockerfile" if index.has("Dockerfile") else None


def _load_package_json(index: RepositoryIndex) -> dict[str, Any] | None:
    if not index.has_metadata("package.json"):
        return None
    text = index.read_text("package.json")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DetectionError(
            "package_json_malformed",
            f"package.json is not valid JSON: {exc.msg}",
            path="package.json",
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    if not isinstance(document, dict):
        raise DetectionError(
            "package_json_malformed",
            "package.json must contain a JSON object",
            path="package.json",
        )
    return document


def _dependencies(package: dict[str, Any] | None) -> set[str]:
    if package is None:
        return set()
    found: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        block = package.get(section)
        if isinstance(block, dict):
            found.update(key for key in block if isinstance(key, str))
    return found


def _has_start_script(package: dict[str, Any] | None) -> bool:
    if package is None:
        return False
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        return False
    start = scripts.get("start")
    return isinstance(start, str) and bool(start.strip())


def _resolve_node_version(
    index: RepositoryIndex, package: dict[str, Any] | None
) -> int:
    """Resolve the Node major: .nvmrc > .node-version > engines.node > default."""
    for filename in NODE_VERSION_FILES:
        if not index.has_metadata(filename):
            continue
        raw = index.read_text(filename).strip()
        major = _node_major(raw)
        if major is not None:
            return major

    engines = package.get("engines") if isinstance(package, dict) else None
    node_range = engines.get("node") if isinstance(engines, dict) else None
    if isinstance(node_range, int):
        if 1 <= node_range <= 99:
            return node_range
    elif isinstance(node_range, str):
        major = _node_major(node_range)
        if major is not None:
            return major

    return DEFAULT_NODE_VERSION


def _node_major(raw: str) -> int | None:
    for token in raw.replace("v", "").split():
        match = _NODE_MAJOR_RE.search(token)
        if match:
            major = int(match.group(1))
            if 1 <= major <= 99:
                return major
    return None


def _unrecognised(index: RepositoryIndex) -> DetectionError:
    detected = tuple(index.paths[:20])
    trailer = "" if len(index.paths) <= 20 else f" (+{len(index.paths) - 20} more)"
    if not index.paths:
        return DetectionError(
            "empty_repository",
            "the source tree is empty: there is nothing to build",
            detected=(),
        )
    expected = (
        "expected one of: a Dockerfile at the source root, index.html, a supported "
        "framework dependency (next, @sveltejs/kit, nuxt, gatsby, astro, vite, remix), "
        "requirements.txt/pyproject.toml, or package.json with a start script"
    )
    return DetectionError(
        "unrecognised_project",
        f"unrecognised project: {expected}. Detected: {', '.join(detected)}{trailer}",
        detected=detected,
    )


def _check_relative_dir(value: str, label: str) -> None:
    if value in ("", "."):
        return
    _check_relative_path(value, label)


def _check_relative_file(value: str, label: str) -> None:
    _check_relative_path(value, label)


def _check_relative_path(value: str, label: str) -> None:
    try:
        normalise_repo_path(value)
    except DetectionError as exc:
        raise DetectionError(
            "invalid_explicit_config", f"{label}: {exc.message}", path=value
        ) from exc


def _normalise_output_dir(value: str | None) -> str | None:
    if value is None:
        return None
    if value in ("", "."):
        return "."
    return normalise_repo_path(value)


def explicit_digest(explicit: ExplicitBuildConfig | None) -> str:
    """Hash the explicit layer for the cache key.

    R-19's formula covers the *tree*, but the explicit layer changes the plan
    without changing the tree. Caching on the tree hash alone would serve one
    customer's custom build command to another project with identical files.
    The tree hash stays spec-exact in `BuildPlan.detectionHash`; the cache key is
    this digest plus that hash.
    """
    if explicit is None:
        payload: dict[str, Any] = {}
    else:
        payload = {
            "strategy": explicit.strategy,
            "dockerfile": explicit.dockerfile,
            "packageManager": explicit.package_manager.value
            if explicit.package_manager
            else None,
            "installCommand": explicit.install_command,
            "buildCommand": explicit.build_command,
            "outputDir": explicit.output_dir,
            "outputKind": explicit.output_kind.value if explicit.output_kind else None,
            "nodeVersion": explicit.node_version,
            "buildpack": explicit.buildpack,
        }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
