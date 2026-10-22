"""Value objects for the SPEC-12 container registry.

The registry is addressed by content, not by a mutable tag. These types keep
that distinction explicit at every boundary: a revision is a human handle, a
digest is the identity, and a storage decision always names the organisation
whose quota it consumes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")
_REPOSITORY_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

REGISTRY_NAMESPACE = "autopilot"
DEFAULT_MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_ORG_STORAGE_BYTES = 20 * 1024 * 1024 * 1024


def validate_digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError("digest must be sha256:<64 lowercase hex characters>")
    return value


def validate_tag(value: str) -> str:
    if not isinstance(value, str) or not _TAG_RE.fullmatch(value):
        raise ValueError(
            "tag must start with a letter, digit or underscore and contain at "
            "most 128 tag characters"
        )
    return value


def validate_slug(value: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.fullmatch(value):
        raise ValueError(
            "slug must be lowercase and contain only letters, digits, '.', '_' "
            "or '-'"
        )
    return value


def validate_repository(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("repository is required")
    parts = value.split("/")
    if len(parts) != 3 or parts[0] != REGISTRY_NAMESPACE:
        raise ValueError(
            f"repository must be {REGISTRY_NAMESPACE}/<org_slug>/<project_slug>"
        )
    for part in parts[1:]:
        if not _REPOSITORY_SEGMENT_RE.fullmatch(part):
            raise ValueError("repository contains an invalid path segment")
    return value


def validate_registry(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("registry host is required")
    if any(character.isspace() for character in value):
        raise ValueError("registry host cannot contain whitespace")
    if "://" in value or "/" in value:
        raise ValueError("registry is a host, not a URL or repository path")
    return value


def repository_for(org_slug: str, project_slug: str) -> str:
    return validate_repository(
        f"{REGISTRY_NAMESPACE}/{validate_slug(org_slug)}/{validate_slug(project_slug)}"
    )


def digest_for(content: bytes) -> str:
    if not isinstance(content, (bytes, bytearray)):
        raise TypeError("image content must be bytes")
    return "sha256:" + hashlib.sha256(bytes(content)).hexdigest()


def revision_for(git_sha: str, at: datetime) -> str:
    """Return the SPEC-12 revision tag for one build.

    ``git_sha`` is shortened to eight characters and the UTC timestamp is
    compact (no colons), so the result is a valid registry tag and is stable
    for a caller that supplies a deterministic clock.
    """

    if not isinstance(git_sha, str) or not _SHA_RE.fullmatch(git_sha):
        raise ValueError("git_sha must be a hexadecimal commit id")
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    timestamp = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    return validate_tag(f"{timestamp}-{git_sha.lower()[:8]}")


@dataclass(frozen=True, slots=True)
class ImageArtifact:
    """The bytes handed to the registry client for one push."""

    content: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, (bytes, bytearray)):
            raise TypeError("image content must be bytes")
        object.__setattr__(self, "content", bytes(self.content))

    @property
    def digest(self) -> str:
        return digest_for(self.content)

    @property
    def size_bytes(self) -> int:
        return len(self.content)


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """Current org storage and the effective ceiling for one push."""

    used_bytes: int
    limit_bytes: int
    image_limit_bytes: int = DEFAULT_MAX_IMAGE_BYTES

    def __post_init__(self) -> None:
        if self.used_bytes < 0:
            raise ValueError("used_bytes cannot be negative")
        if self.limit_bytes < 0:
            raise ValueError("limit_bytes cannot be negative")
        if self.image_limit_bytes <= 0:
            raise ValueError("image_limit_bytes must be positive")

    @property
    def available_bytes(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Everything needed to push and record one immutable image revision."""

    org_id: str
    project_id: str
    deployment_id: str
    registry: str
    repository: str
    tag: str
    artifact: ImageArtifact
    build_image_digest: str
    manifest_hash: str
    credential: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        for name in ("org_id", "project_id", "deployment_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        validate_registry(self.registry)
        validate_repository(self.repository)
        validate_tag(self.tag)
        validate_digest(self.build_image_digest)
        if not isinstance(self.manifest_hash, str) or not self.manifest_hash:
            raise ValueError("manifest_hash is required")

    @property
    def digest(self) -> str:
        return self.artifact.digest

    @property
    def size_bytes(self) -> int:
        return self.artifact.size_bytes


@dataclass(frozen=True, slots=True)
class ImagePublication:
    """The durable facts about a verified push, before storage assigns an id."""

    org_id: str
    project_id: str
    deployment_id: str
    registry: str
    tag: str
    digest: str
    size_bytes: int
    build_image_digest: str
    manifest_hash: str
    repository: str

    def __post_init__(self) -> None:
        validate_registry(self.registry)
        validate_repository(self.repository)
        validate_tag(self.tag)
        validate_digest(self.digest)
        validate_digest(self.build_image_digest)
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class PublishedImage:
    """A row from ``images`` after the push has been committed."""

    id: str
    org_id: str
    project_id: str
    deployment_id: str | None
    tag: str
    digest: str
    registry: str
    size_bytes: int
    build_image_digest: str
    manifest_hash: str
    marked_for_deletion_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_digest(self.digest)
        validate_tag(self.tag)
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")


@dataclass(frozen=True, slots=True)
class RegistryAlert:
    """An operator-facing integrity alert.

    There is deliberately no retry or queue in this type: a digest mismatch is
    evidence to investigate, not work to repeat. Production composition sends
    this to the platform's S2 alert route.
    """

    kind: str
    severity: str
    message: str
    org_id: str | None = None
    project_id: str | None = None
    deployment_id: str | None = None
    details: dict[str, str] | None = None
