"""Pure SPEC-13 rules: the state table, input validation, and drain policy.

Everything here is deterministic and database-free. The service applies these
rules inside the caller's transaction; the tests can verify them without Docker.

State table (SPEC-13 §2):

    queued -> building -> ready -> live
       |        |          |       |
       v        v          v       v
   cancelled  failed     failed  superseded
       \        \          /
        \--------\--------/  terminal: failed, cancelled, superseded

``ready`` and ``live`` are deliberately different facts (R-1). ``superseded``
may return to ``live`` only through an explicit rollback (R-11/R-13), never
through the normal promotion path.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from runtime.lifecycle.errors import InvalidDeployment, InvalidTransition

STATUSES = frozenset(
    {"queued", "building", "ready", "live", "failed", "cancelled", "superseded"}
)
TERMINAL_STATUSES = frozenset({"failed", "cancelled", "superseded"})
# A duplicate webhook is deduplicated while the deployment is still in flight.
# Once it is ready/live/failed/..., the push has finished and a new trigger is
# a legitimate redeploy (SPEC-13 R-10's fourth table row).
ACTIVE_STATUSES = frozenset({"queued", "building"})

TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"building", "cancelled"}),
    "building": frozenset({"ready", "failed", "cancelled"}),
    "ready": frozenset({"live", "failed", "cancelled", "superseded"}),
    "live": frozenset({"superseded"}),
    # Rollback is the only path that resurrects a superseded revision; it is
    # listed here so the pure table describes reality, but no generic caller
    # exposes a "set status" operation.
    "superseded": frozenset({"live"}),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

DEFAULT_DRAIN_SECONDS = 30

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
_IMAGE_RE = re.compile(r"^(?:.+@)?sha256:[0-9a-fA-F]{64}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")
_ACTOR_TYPES = frozenset({"user", "agent", "system"})


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


def is_active(status: str) -> bool:
    return status in ACTIVE_STATUSES


def assert_transition(
    current: str,
    target: str,
    *,
    deployment_id: uuid.UUID | None = None,
) -> None:
    """Raise ``InvalidTransition`` unless ``current -> target`` is in the table.

    Re-applying the same state is a no-op for idempotent retries.
    """
    if current not in STATUSES or target not in STATUSES:
        raise InvalidDeployment(f"unknown deployment state {current!r}/{target!r}")
    if current == target:
        return
    if target not in TRANSITIONS[current]:
        raise InvalidTransition(
            deployment_id or uuid.UUID(int=0), current, target
        )


def assert_ready_invariants(
    status: str, revision: str | None, image_digest: str | None
) -> None:
    """A ``ready`` row needs the verified revision it will serve (AC-1).

    SPEC-03 enforces the revision half in the database. The image digest is
    checked here too because SPEC-12 R-6 makes an unverified push non-ready;
    the registry check itself belongs to SPEC-12.
    """
    if status != "ready":
        return
    if not revision:
        raise InvalidDeployment("ready requires a revision (SPEC-13 R-3/AC-1)")
    if not image_digest:
        raise InvalidDeployment("ready requires a verified image digest (SPEC-12 R-6)")


def assert_terminal_invariants(status: str, finished_at: datetime | None) -> None:
    """A terminal row must carry the time it finished (AC-2)."""
    if is_terminal(status) and finished_at is None:
        raise InvalidDeployment(f"{status} requires finished_at (SPEC-13 R-3/AC-2)")


def assert_actor_type(actor_type: str) -> None:
    if actor_type not in _ACTOR_TYPES:
        raise InvalidDeployment(f"actor_type must be one of {sorted(_ACTOR_TYPES)}")


def validate_actor_id(actor_id: str) -> str:
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise InvalidDeployment("actor_id must be a non-empty string")
    actor_id = actor_id.strip()
    if len(actor_id) > 255 or "\x00" in actor_id:
        raise InvalidDeployment("actor_id must be at most 255 characters without NUL")
    return actor_id


def validate_hostnames(hostnames: Iterable[str]) -> tuple[str, ...]:
    if isinstance(hostnames, (str, bytes)):
        raise InvalidDeployment("hostnames must be a sequence of names")
    cleaned: list[str] = []
    for hostname in hostnames:
        if not isinstance(hostname, str) or not hostname.strip():
            raise InvalidDeployment("hostnames must be non-empty strings")
        hostname = hostname.strip()
        if len(hostname) > 253 or "\x00" in hostname or any(
            character.isspace() for character in hostname
        ):
            raise InvalidDeployment("hostnames must be valid DNS names")
        if hostname not in cleaned:
            cleaned.append(hostname)
    if not cleaned:
        raise InvalidDeployment("hostnames must not be empty")
    return tuple(cleaned)


def validate_git_sha(git_sha: str) -> str:
    if not isinstance(git_sha, str) or not _SHA_RE.fullmatch(git_sha):
        raise InvalidDeployment("git_sha must be 7-64 hexadecimal characters")
    return git_sha.lower()


def validate_git_ref(git_ref: str) -> str:
    if not isinstance(git_ref, str):
        raise InvalidDeployment("git_ref must be a string")
    ref = git_ref.strip()
    if not ref or len(ref) > 1024:
        raise InvalidDeployment("git_ref must be 1-1024 characters")
    if "\x00" in ref or any(character.isspace() for character in ref):
        raise InvalidDeployment("git_ref must not contain whitespace or NUL")
    return ref


def validate_environment(environment: str) -> str:
    if not isinstance(environment, str):
        raise InvalidDeployment("environment must be a string")
    environment = environment.strip()
    if not environment:
        raise InvalidDeployment("environment must be non-empty")
    if "\x00" in environment or any(character.isspace() for character in environment):
        raise InvalidDeployment("environment must not contain whitespace or NUL")
    return environment


def validate_image_reference(image: str) -> str:
    """SPEC-09/SPEC-12: an image is recorded by digest, never by tag."""
    if not isinstance(image, str) or len(image) > 1024:
        raise InvalidDeployment("build_image must be an image reference")
    if not _IMAGE_RE.fullmatch(image):
        raise InvalidDeployment(
            "build_image must be pinned by digest (repo@sha256:<64 hex>)"
        )
    return image


def validate_image_digest(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
        raise InvalidDeployment("image_digest must be sha256:<64 hex>")
    return digest


def validate_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise InvalidDeployment(
            "revision must be 1-255 characters of [A-Za-z0-9._:/-]"
        )
    return revision


def validate_env_keys(keys: Iterable[str]) -> tuple[str, ...]:
    """``build_env_keys`` records names only, never values (SPEC-03 §4.3)."""
    if isinstance(keys, (str, bytes)):
        raise InvalidDeployment("build_env_keys must be a sequence of names")
    cleaned: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not _ENV_KEY_RE.fullmatch(key):
            raise InvalidDeployment(
                "build_env_keys must be environment-variable names (not values)"
            )
        if key not in cleaned:
            cleaned.append(key)
    return tuple(cleaned)


def idempotency_key(
    project_id: uuid.UUID, git_sha: str, git_ref: str
) -> str:
    """R-10: ``(project_id, git_sha, git_ref)`` - the ref is load-bearing (R-7)."""
    return f"deploy:{project_id}:{git_sha}:{git_ref}"


def promotion_lock_key(project_id: uuid.UUID, git_ref: str) -> str:
    """Serialise cutovers for one ref without blocking other refs (R-7)."""
    return f"promote:{project_id}:{git_ref}"


def canonical_manifest_json(manifest: Mapping[str, Any] | Any) -> tuple[str, str]:
    """Return ``(canonical_json, sha256:<hex>)`` for the resolved manifest.

    SPEC-06 R-3 says ``manifest_hash`` is stored; it does not define the
    canonicalisation. Until the shared contract owns it (proposal logged in
    DECISIONS.md), JSON with sorted keys is the deterministic form. Callers that
    already have an authoritative hash pass it to ``trigger`` instead.
    """
    if hasattr(manifest, "model_dump"):
        try:
            payload: Any = manifest.model_dump(mode="json", exclude_none=True)
        except Exception as exc:  # pydantic must not leak a value-bearing error
            raise InvalidDeployment("manifest could not be serialised") from exc
    elif isinstance(manifest, Mapping):
        payload = dict(manifest)
    else:
        raise InvalidDeployment("manifest must be a mapping or a manifest model")

    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    except (TypeError, ValueError) as exc:
        raise InvalidDeployment("manifest must be JSON-serialisable") from exc
    digest = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def canonical_manifest_hash(manifest: Mapping[str, Any] | Any) -> str:
    return canonical_manifest_json(manifest)[1]


def validate_manifest_hash(manifest_hash: str) -> str:
    if not isinstance(manifest_hash, str) or not _DIGEST_RE.fullmatch(manifest_hash):
        raise InvalidDeployment("manifest_hash must be sha256:<64 hex>")
    return manifest_hash


@dataclass(frozen=True)
class DrainPolicy:
    """R-14/R-15: draining waits for in-flight requests, but never forever.

    SPEC-14 performs the actual drain; this value is the bound it must honour.
    Edge components reject long-lived connections (SPEC-17 R-9) rather than
    letting one hold a drain open.
    """

    max_request_duration_seconds: int = DEFAULT_DRAIN_SECONDS

    def __post_init__(self) -> None:
        if isinstance(self.max_request_duration_seconds, bool) or not isinstance(
            self.max_request_duration_seconds, int
        ):
            raise InvalidDeployment("max_request_duration_seconds must be an integer")
        if self.max_request_duration_seconds < 1:
            raise InvalidDeployment("max_request_duration_seconds must be >= 1")

    @property
    def grace_seconds(self) -> int:
        return self.max_request_duration_seconds


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_DRAIN_SECONDS",
    "DrainPolicy",
    "STATUSES",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "assert_actor_type",
    "assert_ready_invariants",
    "assert_terminal_invariants",
    "assert_transition",
    "canonical_manifest_hash",
    "canonical_manifest_json",
    "idempotency_key",
    "is_active",
    "is_terminal",
    "promotion_lock_key",
    "validate_actor_id",
    "validate_env_keys",
    "validate_environment",
    "validate_git_ref",
    "validate_git_sha",
    "validate_hostnames",
    "validate_image_digest",
    "validate_image_reference",
    "validate_manifest_hash",
    "validate_revision",
]
