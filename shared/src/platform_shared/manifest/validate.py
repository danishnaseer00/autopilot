"""Manifest parsing and validation (SPEC-06 R-4..R-14, §5 failure modes).

Every rejection names the JSON path of the problem, e.g.
`services[1].dependsOn references unknown service 'db'` (R-4). Errors are
collected across all checks so one round fixes many mistakes; the build plane
must treat a non-empty error list as fatal before any resource is created.

Sections: YAML loading, structural parse, semantic checks, result type.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from platform_shared.manifest.models import (
    KNOWN_TOP_LEVEL_KEYS,
    ManifestDocument,
)

SUPPORTED_MANIFEST_VERSIONS = {1}  # R-13: a higher version fails; we do not guess
MAX_MANIFEST_BYTES = 256 * 1024   # §5: a manifest is configuration, not data

# R-9: reserved names - the platform control plane must never be shadowed.
RESERVED_SERVICE_NAMES = frozenset(
    {
        "autopilot",
        "platform",
        "kube",
        "registry",
        "grafana",
        "prometheus",
        "loki",
        "tempo",
        "alertmanager",
        "ingress",
        "cert-manager",
        "cert_manager",
    }
)

# SPEC-02 R-5: env values are secret references, never literals.
_ENV_VALUE_RE = re.compile(r"^\$\{[A-Z_][A-Z0-9_]*\}$")

_ERROR = tuple[str, str]  # (json_path, message)


class ManifestYamlError(Exception):
    """Invalid YAML. Carries line/column so the API can answer 422, never 500."""

    def __init__(self, message: str, line: int | None = None, column: int | None = None):
        self.line = line
        self.column = column
        super().__init__(message)


class ManifestValidationError(Exception):
    """Structural/semantic validation failed. `errors` name JSON paths."""

    def __init__(self, errors: list[_ERROR], warnings: list[str] | None = None):
        self.errors = errors
        self.warnings = warnings or []
        super().__init__(
            "; ".join(f"{p}: {m}" for p, m in errors) or "manifest invalid"
        )


class ValidatedManifest:
    def __init__(self, manifest: ManifestDocument, warnings: list[str]):
        self.manifest = manifest
        self.warnings = warnings

    @property
    def services(self):
        return self.manifest.services


def parse_manifest(text: str) -> ValidatedManifest:
    """Parse raw YAML/JSON text into a validated document, or raise."""
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestValidationError(
            [("<manifest>", f"manifest exceeds {MAX_MANIFEST_BYTES}-byte size limit")]
        )

    document = _load_yaml(text)
    if not isinstance(document, dict):
        raise ManifestValidationError([("<root>", "manifest must be a mapping")])

    errors: list[_ERROR] = []
    warnings: list[str] = [
        f"unknown top-level key '{k}' ignored"  # R-14: warn, still deploy
        for k in sorted(set(document) - set(KNOWN_TOP_LEVEL_KEYS))
    ]

    version = document.get("version")
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise ManifestValidationError(
            [(
                "version",
                f"manifest version {version!r} is not supported; "
                f"supported versions: {sorted(SUPPORTED_MANIFEST_VERSIONS)}",
            )],
            warnings,
        )

    manifest = _structurally_parse(document, warnings, errors)
    _semantic_checks(manifest, errors)
    if errors:
        raise ManifestValidationError(errors, warnings)
    return ValidatedManifest(manifest, warnings)


def _load_yaml(text: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            # yaml marks are 0-based; humans and HTTP problems report 1-based
            raise ManifestYamlError(
                f"invalid YAML: {exc.problem or exc}",
                line=mark.line + 1,
                column=mark.column + 1,
            ) from exc
        raise ManifestYamlError(f"invalid YAML: {exc}") from exc


def _structurally_parse(
    document: dict, warnings: list[str], errors: list[_ERROR]
) -> ManifestDocument:
    try:
        return ManifestDocument.model_validate(document)
    except Exception as exc:  # pydantic.ValidationError
        raise ManifestValidationError(_pydantic_errors(exc), warnings) from exc


def _pydantic_errors(exc: Exception) -> list[_ERROR]:
    from pydantic import ValidationError

    if not isinstance(exc, ValidationError):
        return [("<root>", str(exc))]
    out: list[_ERROR] = []
    for err in exc.errors():
        path = _loc_to_path(err["loc"])
        msg = err["msg"]
        if err["type"] == "extra_forbidden" and err.get("input") is not None:
            msg = f"unknown key {str(err['input'])!r} is not allowed here"
        out.append((path, msg))
    return out


def _loc_to_path(loc: tuple[Any, ...]) -> str:
    parts = ""
    for i, piece in enumerate(loc):
        if isinstance(piece, int):
            parts += f"[{piece}]"
        elif parts:
            parts += f".{piece}"
        else:
            parts = str(piece)
    return parts


def _semantic_checks(manifest: ManifestDocument, errors: list[_ERROR]) -> None:
    services = manifest.services
    if not services:
        errors.append(("services", "at least one service is required"))

    by_name = {s.name: i for i, s in enumerate(services)}

    for i, svc in enumerate(services):
        path = lambda suffix: f"services[{i}].{suffix}"  # noqa: E731 - local shorthand

        if svc.name in RESERVED_SERVICE_NAMES or svc.name.startswith("autopilot-"):
            errors.append(
                (
                    path("name"),
                    f"service name '{svc.name}' is reserved for platform use (R-9)",
                )
            )

        if svc.image and svc.build is not None:
            errors.append(
                (
                    path("build"),
                    f"service '{svc.name}' sets both image and build; "
                    "is it built or pulled? (R-7)",
                )
            )

        if svc.type == "stateful" and (svc.runtime.persistentVolume is None):
            errors.append(
                (
                    path("runtime.persistentVolume"),
                    f"stateful service '{svc.name}' requires runtime.persistentVolume "
                    "- data loss otherwise",
                )
            )
        if svc.type == "stateful" and svc.image is None and svc.build is None:
            errors.append(
                (
                    path("image"),
                    f"stateful service '{svc.name}' has no image and no build; "
                    "there is nothing to run",
                )
            )

        requests = svc.resources.requests if svc.resources else None
        if requests is None or requests.cpu is None or requests.memory is None:
            errors.append(
                (
                    path("resources.requests"),
                    f"service '{svc.name}' is missing resources.requests "
                    "(cpu and memory are required) (R-8)",
                )
            )

        for key, value in svc.env.items():
            if not _ENV_VALUE_RE.match(value):
                errors.append(
                    (
                        path(f"env.{key}"),
                        f"env values must reference a secret like '${{NAME}}' "
                        f"(SPEC-02 R-5), got {value!r}",
                    )
                )

        for dep in svc.dependsOn:
            if dep not in by_name:
                errors.append(
                    (
                        path("dependsOn"),
                        f"dependsOn references unknown service '{dep}' (R-4)",
                    )
                )

    _check_cycles(services, errors)

    exposure = manifest.exposure
    if exposure is not None:
        seen_paths: dict[str, int] = {}
        for i, route in enumerate(exposure.routes):
            rpath = f"exposure.routes[{i}]"
            if not route.path.startswith("/"):
                errors.append((f"{rpath}.path", f"route path must start with '/', got {route.path!r}"))
            if route.path in seen_paths:
                errors.append(
                    (
                        f"{rpath}.path",
                        f"duplicate route path {route.path!r} (routes[{seen_paths[route.path]}])",
                    )
                )
            seen_paths[route.path] = i
            if route.service not in by_name:
                errors.append(
                    (
                        f"{rpath}.service",
                        f"route points at unknown service '{route.service}' (R-6)",
                    )
                )


def _check_cycles(services, errors: list[_ERROR]) -> None:
    index = {s.name: i for i, s in enumerate(services)}
    # iterative DFS with a on-stack marker; report the full cycle path (R-5)
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {name: WHITE for name in index}
    for start in index:
        if colour[start] != WHITE:
            continue
        stack = [(start, iter(services[index[start]].dependsOn))]
        colour[start] = GREY
        trail = [start]
        while stack:
            node, deps = stack[-1]
            advanced = False
            for dep in deps:  # continue where the iterator left off
                if dep not in index:  # unknown refs already reported
                    continue
                state = colour[dep]
                if state == GREY:
                    cycle = trail[trail.index(dep):] + [dep]
                    i = index[node]
                    errors.append(
                        (
                            f"services[{i}].dependsOn",
                            "dependsOn cycle: " + " -> ".join(cycle) + " (R-5)",
                        )
                    )
                    continue
                if state == WHITE:
                    colour[dep] = GREY
                    trail.append(dep)
                    stack.append((dep, iter(services[index[dep]].dependsOn)))
                    advanced = True
                    break
            if advanced:
                continue
            # exhausted this node's dependencies
            stack.pop()
            colour[node] = BLACK
            if trail and trail[-1] == node:
                trail.pop()
