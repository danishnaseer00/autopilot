"""platform_shared: contracts shared across planes and teams.

Currently hosts the app manifest (SPEC-06) and the derived dependency graph.
Every consumer (build plane, runtime plane, RCA, console) imports from here -
never re-implement the manifest format.
"""

from platform_shared.manifest import (
    GraphDoc,
    ManifestDocument,
    ManifestValidationError,
    ManifestYamlError,
    ValidatedManifest,
    build_graph,
    parse_manifest,
)

__all__ = [
    "GraphDoc",
    "ManifestDocument",
    "ManifestValidationError",
    "ManifestYamlError",
    "ValidatedManifest",
    "build_graph",
    "parse_manifest",
]
