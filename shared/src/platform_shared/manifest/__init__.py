"""The app manifest contract (SPEC-06). This module is the format's single home:
the PaaS renders it into Kubernetes objects, the RCA component derives the
service-dependency graph from parsed objects, and the console validates input -
all from these types, never from a local re-implementation.

R-15: changes to SPEC-06 need both owners' sign-off. R-17: the fixture test in
shared/tests must keep passing for both suites.
"""

from platform_shared.manifest.graph import GraphDoc, build_graph
from platform_shared.manifest.hash import (
    ManifestHashError,
    canonical_manifest_json,
    manifest_hash,
)
from platform_shared.manifest.models import ManifestDocument
from platform_shared.manifest.validate import (
    ManifestValidationError,
    ManifestYamlError,
    ValidatedManifest,
    parse_manifest,
)

__all__ = [
    "GraphDoc",
    "ManifestDocument",
    "ManifestHashError",
    "ManifestValidationError",
    "ManifestYamlError",
    "ValidatedManifest",
    "build_graph",
    "canonical_manifest_json",
    "manifest_hash",
    "parse_manifest",
]
