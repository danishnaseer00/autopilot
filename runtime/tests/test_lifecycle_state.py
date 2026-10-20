"""SPEC-13 pure rules: transitions, invariants, validation and hashing."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from platform_shared import parse_manifest
from runtime.lifecycle.errors import InvalidDeployment, InvalidTransition
from runtime.lifecycle.state import (
    DEFAULT_DRAIN_SECONDS,
    DrainPolicy,
    assert_actor_type,
    assert_ready_invariants,
    assert_terminal_invariants,
    assert_transition,
    canonical_manifest_hash,
    canonical_manifest_json,
    idempotency_key,
    is_active,
    is_terminal,
    promotion_lock_key,
    validate_actor_id,
    validate_env_keys,
    validate_environment,
    validate_git_ref,
    validate_git_sha,
    validate_hostnames,
    validate_image_digest,
    validate_image_reference,
    validate_manifest_hash,
    validate_revision,
)

SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "ghcr.io/autopilot/build-node@" + DIGEST


def test_transition_table_matches_the_spec() -> None:
    allowed = {
        ("queued", "building"),
        ("queued", "cancelled"),
        ("building", "ready"),
        ("building", "failed"),
        ("building", "cancelled"),
        ("ready", "live"),
        ("ready", "failed"),
        ("ready", "cancelled"),
        ("ready", "superseded"),
        ("live", "superseded"),
        ("superseded", "live"),  # rollback only
    }
    for current, target in allowed:
        assert_transition(current, target)

    for current, target in {
        ("queued", "ready"),
        ("queued", "live"),
        ("building", "live"),
        ("ready", "building"),
        ("live", "failed"),
        ("live", "cancelled"),
        ("failed", "live"),
        ("cancelled", "building"),
    }:
        with pytest.raises(InvalidTransition):
            assert_transition(current, target)


def test_reapplying_a_state_is_idempotent() -> None:
    for status in ("queued", "building", "ready", "live", "failed", "cancelled", "superseded"):
        assert_transition(status, status)


def test_terminal_and_active_classification() -> None:
    assert {status for status in ("failed", "cancelled", "superseded") if is_terminal(status)} == {
        "failed",
        "cancelled",
        "superseded",
    }
    assert not is_terminal("ready")
    assert not is_terminal("live")
    assert is_active("queued") and is_active("building")
    assert not is_active("ready")


def test_ready_requires_revision_and_digest() -> None:
    with pytest.raises(InvalidDeployment):
        assert_ready_invariants("ready", None, DIGEST)
    with pytest.raises(InvalidDeployment):
        assert_ready_invariants("ready", "rev-1", None)
    assert_ready_invariants("ready", "rev-1", DIGEST)
    assert_ready_invariants("building", None, None)  # other states are untouched


def test_terminal_requires_finished_at() -> None:
    with pytest.raises(InvalidDeployment):
        assert_terminal_invariants("failed", None)
    with pytest.raises(InvalidDeployment):
        assert_terminal_invariants("superseded", None)
    assert_terminal_invariants(
        "cancelled", datetime.now(tz=timezone.utc)
    )
    assert_terminal_invariants("ready", None)  # not terminal


def test_drain_policy_default_and_bounds() -> None:
    policy = DrainPolicy()
    assert policy.max_request_duration_seconds == DEFAULT_DRAIN_SECONDS == 30
    assert policy.grace_seconds == 30
    assert DrainPolicy(5).grace_seconds == 5
    with pytest.raises(InvalidDeployment):
        DrainPolicy(0)
    with pytest.raises(InvalidDeployment):
        DrainPolicy(-1)  # type: ignore[arg-type]
    with pytest.raises(InvalidDeployment):
        DrainPolicy(True)  # type: ignore[arg-type]


def test_git_identity_validation() -> None:
    assert validate_git_sha(SHA) == SHA
    assert validate_git_sha(SHA.upper()) == SHA
    assert validate_git_sha("abc1234") == "abc1234"
    for bad in ("", "abc123", "z" * 40, "a" * 65):
        with pytest.raises(InvalidDeployment):
            validate_git_sha(bad)
    assert validate_git_ref("refs/heads/main") == "refs/heads/main"
    with pytest.raises(InvalidDeployment):
        validate_git_ref("refs/heads/two words")
    with pytest.raises(InvalidDeployment):
        validate_git_ref("")
    assert validate_environment("production") == "production"
    with pytest.raises(InvalidDeployment):
        validate_environment("  ")


def test_images_must_be_digest_pinned() -> None:
    assert validate_image_reference(IMAGE) == IMAGE
    assert validate_image_reference(DIGEST) == DIGEST
    for bad in (
        "ghcr.io/autopilot/build-node:latest",
        "ghcr.io/autopilot/build-node@sha256:short",
        "",
    ):
        with pytest.raises(InvalidDeployment):
            validate_image_reference(bad)
    assert validate_image_digest(DIGEST) == DIGEST
    with pytest.raises(InvalidDeployment):
        validate_image_digest("sha256:short")


def test_revision_validation() -> None:
    assert validate_revision("sha256:" + "c" * 64) == "sha256:" + "c" * 64
    assert validate_revision("rev_2026.10") == "rev_2026.10"
    with pytest.raises(InvalidDeployment):
        validate_revision("has space")
    with pytest.raises(InvalidDeployment):
        validate_revision("")


def test_build_env_keys_are_names_only() -> None:
    assert validate_env_keys(["DATABASE_URL", "REDIS_URL", "DATABASE_URL"]) == (
        "DATABASE_URL",
        "REDIS_URL",
    )
    assert validate_env_keys([]) == ()
    with pytest.raises(InvalidDeployment):
        validate_env_keys(["DATABASE_URL=postgres://secret"])
    with pytest.raises(InvalidDeployment):
        validate_env_keys(["1BAD"])
    with pytest.raises(InvalidDeployment):
        validate_env_keys("DATABASE_URL")


def test_idempotency_and_promotion_keys_include_the_ref() -> None:
    project = uuid.uuid4()
    first = idempotency_key(project, SHA, "refs/heads/main")
    second = idempotency_key(project, SHA, "refs/pull/7/head")
    assert first != second
    assert SHA in first and "refs/heads/main" in first
    assert promotion_lock_key(project, "refs/heads/main") != promotion_lock_key(
        project, "refs/pull/7/head"
    )


def test_manifest_hash_is_order_independent() -> None:
    left = {"b": 2, "a": {"y": 1, "x": 0}}
    right = {"a": {"x": 0, "y": 1}, "b": 2}
    left_json, left_hash = canonical_manifest_json(left)
    right_json, right_hash = canonical_manifest_json(right)
    assert left_json == right_json
    assert left_hash == right_hash
    assert left_hash.startswith("sha256:") and len(left_hash) == 71
    assert validate_manifest_hash(left_hash) == left_hash
    with pytest.raises(InvalidDeployment):
        canonical_manifest_json({"when": object()})
    with pytest.raises(InvalidDeployment):
        validate_manifest_hash("md5:abc")


def test_manifest_hash_uses_the_shared_spec06_model() -> None:
    parsed = parse_manifest(
        """
version: 1
name: shop
services:
  - name: web
    source: .
    type: node
    runtime:
      port: 3000
    resources:
      requests: { cpu: 100m, memory: 128Mi }
"""
    )
    assert parsed.warnings == []
    first = canonical_manifest_hash(parsed.manifest)
    second = canonical_manifest_hash(parsed.manifest)
    assert first == second
    assert first.startswith("sha256:")


def test_hostname_and_actor_validation() -> None:
    assert validate_hostnames(["a.example.com", "a.example.com", "b.example.com"]) == (
        "a.example.com",
        "b.example.com",
    )
    for bad in ([], [""], ["bad host"], ["a\x00b"]):
        with pytest.raises(InvalidDeployment):
            validate_hostnames(bad)
    with pytest.raises(InvalidDeployment):
        validate_hostnames("a.example.com")  # type: ignore[arg-type]

    assert validate_actor_id("  alice  ") == "alice"
    for bad in ("", "   ", "a\x00b"):
        with pytest.raises(InvalidDeployment):
            validate_actor_id(bad)
    assert_actor_type("user")
    assert_actor_type("system")
    with pytest.raises(InvalidDeployment):
        assert_actor_type("root")
