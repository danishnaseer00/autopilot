"""Namespace scoping for fault injection (SPEC-30, SPEC-14 R-1/R-5).

Fault injection is destructive by construction.  It may only ever touch a
dedicated test namespace; a tenant application namespace and every platform
namespace are off limits.  The rule is intentionally a positive allow-list:
``autopilot-test-<slug>`` and nothing else.  "Never platform namespaces" is then
a consequence of the prefix, not a denylist that can be bypassed by a new
platform name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from autopilot.inject.errors import ArtifactScopeError

TEST_NAMESPACE_PREFIX = "autopilot-test-"
"""Only dedicated evaluation namespaces use this prefix."""

# Explicit platform names are listed for a clearer error message and as a
# second check.  The allow-list already makes them unreachable.
PLATFORM_NAMESPACES = frozenset(
    {
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "autopilot-system",
        "autopilot-control",
        "autopilot-build",
        "autopilot-runtime",
        "autopilot-ingress",
        "autopilot-monitoring",
        "autopilot-observability",
        "autopilot-registry",
        "chaos-mesh",
    }
)

_DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_PLATFORM_PREFIXES = ("autopilot-", "kube-")


def validate_test_namespace(namespace: object) -> str:
    """Return *namespace* if it is a dedicated test namespace, else raise.

    This is the single gate used by the plan, Chaos Mesh renderer, application
    hook renderer and every generated artifact.
    """

    if not isinstance(namespace, str) or not namespace:
        raise ArtifactScopeError("namespace must be a non-empty string")
    if len(namespace) > 63:
        raise ArtifactScopeError(
            f"namespace {namespace!r} exceeds the Kubernetes 63-character label limit"
        )
    if not _DNS_LABEL.match(namespace):
        raise ArtifactScopeError(
            f"namespace {namespace!r} is not a valid DNS-1123 label"
        )
    if namespace in PLATFORM_NAMESPACES:
        raise ArtifactScopeError(
            f"namespace {namespace!r} is a platform namespace; fault injection is forbidden"
        )
    if not namespace.startswith(TEST_NAMESPACE_PREFIX):
        raise ArtifactScopeError(
            f"namespace {namespace!r} is not a dedicated test namespace "
            f"(expected prefix {TEST_NAMESPACE_PREFIX!r})"
        )
    suffix = namespace[len(TEST_NAMESPACE_PREFIX) :]
    if not suffix:
        raise ArtifactScopeError(
            f"namespace {namespace!r} has no test-application identifier"
        )
    if namespace.startswith(_PLATFORM_PREFIXES) and not namespace.startswith(
        TEST_NAMESPACE_PREFIX
    ):
        raise ArtifactScopeError(
            f"namespace {namespace!r} looks like a platform namespace"
        )
    return namespace


def is_test_namespace(namespace: object) -> bool:
    """Return whether *namespace* passes :func:`validate_test_namespace`."""

    try:
        validate_test_namespace(namespace)
    except ArtifactScopeError:
        return False
    return True


def _walk_namespace_fields(value: Any) -> Iterable[tuple[str, Any]]:
    """Yield every Kubernetes namespace-bearing field in a document."""

    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"namespace", "namespaces"}:
                yield str(key), child
            elif key == "namespaceSelector":
                # A namespaceSelector can select platform namespaces by label.
                yield "namespaceSelector", child
            else:
                yield from _walk_namespace_fields(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_namespace_fields(child)


def assert_artifact_scope(
    document: Mapping[str, Any], *, expected_namespace: str | None = None
) -> None:
    """Fail unless every namespace reference in *document* is a test namespace.

    ``expected_namespace`` additionally pins every reference to one namespace,
    which is what a single-run fault artifact requires.  ``namespaceSelector``
    is rejected outright: a selector can reach any namespace with matching
    labels and cannot be audited as a test-only scope.
    """

    if expected_namespace is not None:
        validate_test_namespace(expected_namespace)

    found = 0
    for field, value in _walk_namespace_fields(document):
        if field == "namespaceSelector":
            raise ArtifactScopeError(
                "namespaceSelector is forbidden in fault-injection artifacts"
            )
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            if not isinstance(item, str):
                raise ArtifactScopeError(
                    f"{field} must contain strings, got {type(item).__name__}"
                )
            validate_test_namespace(item)
            if expected_namespace is not None and item != expected_namespace:
                raise ArtifactScopeError(
                    f"artifact references namespace {item!r}, expected {expected_namespace!r}"
                )
            found += 1

    if expected_namespace is not None and found == 0:
        raise ArtifactScopeError(
            "artifact has no namespace field; scope cannot be proven"
        )
