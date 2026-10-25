"""SPEC-30 namespace scoping: only dedicated test namespaces are reachable."""

from __future__ import annotations

import pytest

from autopilot.inject.errors import ArtifactScopeError
from autopilot.inject.namespaces import (
    PLATFORM_NAMESPACES,
    TEST_NAMESPACE_PREFIX,
    assert_artifact_scope,
    is_test_namespace,
    validate_test_namespace,
)


@pytest.mark.parametrize(
    "namespace",
    [
        "autopilot-test-storefront",
        "autopilot-test-a",
        "autopilot-test-123",
        "autopilot-test-recovery-check",
    ],
)
def test_dedicated_test_namespaces_are_allowed(namespace: str) -> None:
    assert validate_test_namespace(namespace) == namespace
    assert is_test_namespace(namespace)


@pytest.mark.parametrize(
    "namespace",
    [
        "default",
        "kube-system",
        "kube-public",
        "kube-node-lease",
        "chaos-mesh",
        "autopilot-system",
        "autopilot-runtime",
        "autopilot-control",
        "autopilot-build",
        "production",
        "tenant-app",
    ],
)
def test_platform_and_tenant_namespaces_are_rejected(namespace: str) -> None:
    # A denylist entry and anything outside the positive allow-list.
    assert namespace in PLATFORM_NAMESPACES or not namespace.startswith(
        TEST_NAMESPACE_PREFIX
    )
    with pytest.raises(ArtifactScopeError):
        validate_test_namespace(namespace)
    assert not is_test_namespace(namespace)


@pytest.mark.parametrize(
    "namespace",
    [
        "",
        "autopilot-test-",
        "Autopilot-Test-Store",
        "autopilot_test_x",
        "autopilot-test-a.b",
        "autopilot-test--",
        "autopilot-test-a" + "b" * 60,
    ],
)
def test_malformed_namespaces_are_rejected(namespace: str) -> None:
    with pytest.raises(ArtifactScopeError):
        validate_test_namespace(namespace)


def test_non_strings_are_rejected() -> None:
    for value in (None, 3, [], {}):
        with pytest.raises(ArtifactScopeError):
            validate_test_namespace(value)


def test_artifact_with_one_test_namespace_passes() -> None:
    document = {
        "metadata": {"namespace": "autopilot-test-store"},
        "spec": {"selector": {"namespaces": ["autopilot-test-store"]}},
    }
    assert_artifact_scope(document, expected_namespace="autopilot-test-store")


def test_artifact_scope_walks_nested_documents() -> None:
    document = {
        "spec": {
            "selector": {
                "namespaces": ["autopilot-test-store"],
                "nested": [{"namespace": "autopilot-test-store"}],
            }
        }
    }
    assert_artifact_scope(document, expected_namespace="autopilot-test-store")


def test_artifact_referencing_two_namespaces_is_rejected() -> None:
    document = {
        "metadata": {"namespace": "autopilot-test-a"},
        "spec": {"selector": {"namespaces": ["autopilot-test-b"]}},
    }
    with pytest.raises(ArtifactScopeError, match="expected"):
        assert_artifact_scope(document, expected_namespace="autopilot-test-a")


def test_artifact_without_a_namespace_cannot_prove_scope() -> None:
    with pytest.raises(ArtifactScopeError, match="no namespace"):
        assert_artifact_scope({"kind": "PodChaos"}, expected_namespace="autopilot-test-a")


def test_artifact_with_platform_namespace_is_rejected() -> None:
    document = {"metadata": {"namespace": "kube-system"}}
    with pytest.raises(ArtifactScopeError, match="platform namespace"):
        assert_artifact_scope(document)


def test_namespace_selector_is_rejected_outright() -> None:
    # A selector can reach any namespace by label and is not auditable.
    document = {
        "metadata": {"namespace": "autopilot-test-a"},
        "spec": {"selector": {"namespaceSelector": {"matchLabels": {"team": "x"}}}},
    }
    with pytest.raises(ArtifactScopeError, match="namespaceSelector"):
        assert_artifact_scope(document, expected_namespace="autopilot-test-a")


def test_namespace_values_must_be_strings() -> None:
    document = {"metadata": {"namespace": 7}}
    with pytest.raises(ArtifactScopeError, match="strings"):
        assert_artifact_scope(document)


def test_expected_namespace_must_itself_be_a_test_namespace() -> None:
    with pytest.raises(ArtifactScopeError):
        assert_artifact_scope(
            {"metadata": {"namespace": "autopilot-test-a"}},
            expected_namespace="default",
        )
