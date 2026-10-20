"""The pure SPEC-14 renderer (R-9).

``render_manifest`` is the whole public surface for turning a validated SPEC-06
manifest into Kubernetes objects. It makes no cluster call, reads no clock and
uses no randomness. ``Rendered.json()`` canonicalises the object list (sorted
keys, compact separators), so two calls with equal input are byte-identical.

Manifest validation is delegated to :mod:`platform_shared.manifest`; this module
must never grow a second set of manifest rules (SPEC-06 R-15).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import yaml

from platform_shared.manifest import (
    ManifestDocument,
    ManifestValidationError,
    ManifestYamlError,
    parse_manifest,
)

from runtime.k8srender.context import RenderContext
from runtime.k8srender.errors import RenderError
from runtime.k8srender.isolation import render_isolation
from runtime.k8srender.workloads import (
    referenced_env_keys,
    render_workloads,
    service_ports,
    workload_refs,
)

_KIND_PRIORITY: Mapping[str, int] = {
    "Namespace": 0,
    "ResourceQuota": 1,
    "LimitRange": 2,
    "NetworkPolicy": 3,
    "Role": 4,
    "RoleBinding": 5,
    "ConfigMap": 6,
    "Service": 7,
    "Deployment": 8,
    "StatefulSet": 8,
}


@dataclass(frozen=True)
class Rendered:
    """A deterministic object set and the metadata a deployer needs."""

    objects: tuple[Mapping[str, Any], ...]
    workloads: tuple[Mapping[str, str], ...]
    service_ports: tuple[int, ...]
    warnings: tuple[str, ...] = ()

    def json(self) -> str:
        return json.dumps(
            list(self.objects),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def object_names(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (
                str(obj["kind"]),
                str(obj["metadata"].get("namespace", "")),
                str(obj["metadata"]["name"]),
            )
            for obj in self.objects
        )


def _document_dict(manifest: Any) -> dict[str, Any]:
    if hasattr(manifest, "manifest") and hasattr(manifest.manifest, "model_dump"):
        manifest = manifest.manifest
    if isinstance(manifest, ManifestDocument):
        return manifest.model_dump(mode="json", exclude_none=True)
    if isinstance(manifest, Mapping):
        return dict(manifest)
    raise RenderError("manifest must be a ManifestDocument or a mapping")


def validate_for_render(manifest: Any) -> tuple[ManifestDocument, tuple[str, ...]]:
    """Run the shared SPEC-06 validator and return the document and warnings."""
    document = _document_dict(manifest)
    try:
        text = yaml.safe_dump(document, sort_keys=True, allow_unicode=True)
    except yaml.YAMLError as exc:
        raise RenderError("manifest could not be serialised for validation") from exc
    try:
        validated = parse_manifest(text)
    except (ManifestValidationError, ManifestYamlError) as exc:
        # The shared error already names JSON paths and never contains values.
        raise RenderError(str(exc)) from exc
    return validated.manifest, tuple(validated.warnings)


def _sort_key(obj: Mapping[str, Any]) -> tuple[int, str, str, str]:
    kind = str(obj.get("kind", ""))
    meta = obj.get("metadata", {})
    return (
        _KIND_PRIORITY.get(kind, 99),
        kind,
        str(meta.get("namespace", "")),
        str(meta.get("name", "")),
    )


def render_manifest(manifest: Any, ctx: RenderContext) -> Rendered:
    """Render a manifest into a deterministic, apply-ordered object list."""
    document, warnings = validate_for_render(manifest)
    ports = service_ports(document)

    objects = [
        *render_isolation(ctx, ports),
        *render_workloads(document, ctx),
    ]
    objects.sort(key=_sort_key)

    seen: set[tuple[str, str, str]] = set()
    for obj in objects:
        key = (
            str(obj.get("kind")),
            str(obj.get("metadata", {}).get("namespace", "")),
            str(obj.get("metadata", {}).get("name")),
        )
        if key in seen:
            raise RenderError(
                f"rendered duplicate object {key[0]} {key[1]}/{key[2]}"
            )
        seen.add(key)

    workloads = tuple(sorted(workload_refs(document, ctx), key=lambda ref: ref["name"]))
    return Rendered(
        objects=tuple(objects),
        workloads=workloads,
        service_ports=ports,
        warnings=warnings,
    )


def render_json(manifest: Any, ctx: RenderContext) -> str:
    return render_manifest(manifest, ctx).json()


def secret_keys_for(manifest: Any) -> tuple[str, ...]:
    """The project Secret keys a deployment references (names only)."""
    document, _warnings = validate_for_render(manifest)
    return referenced_env_keys(document)


__all__ = [
    "Rendered",
    "render_json",
    "render_manifest",
    "secret_keys_for",
    "validate_for_render",
]
