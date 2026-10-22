"""Registry client port and the in-memory reference registry.

The production adapter talks to the in-cluster ``registry:3`` HTTP API. This
module keeps the policy assertions testable without Docker: immutable tags are
enforced by the registry object (not by the publisher), and every pull is
checked against a scoped credential before content is returned.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from buildplane.registry.credentials import RegistryCredentialIssuer
from buildplane.registry.errors import (
    ContainerRegistryUnavailable,
    ImmutableTagConflict,
    ImageDigestMismatch,
    ImageTooLarge,
    PullAccessDenied,
    PushAccessDenied,
)
from buildplane.registry.models import (
    DEFAULT_MAX_IMAGE_BYTES,
    digest_for,
    validate_digest,
    validate_repository,
    validate_tag,
)


class ContainerRegistry(Protocol):
    async def push(
        self,
        *,
        repository: str,
        tag: str,
        digest: str,
        content: bytes,
        credential: str | None = None,
    ) -> str: ...

    async def pull(
        self,
        *,
        repository: str,
        reference: str,
        credential: str | None = None,
    ) -> bytes: ...

    async def delete(self, *, repository: str, reference: str) -> bool: ...


class InMemoryContainerRegistry:
    """Reference registry used by unit tests and local composition.

    It deliberately behaves like a real registry for the properties SPEC-12
    cares about: a tag is immutable, a push returns the content digest, and a
    pull token can only read the repository namespace it was minted for.
    """

    def __init__(
        self,
        *,
        credentials: RegistryCredentialIssuer | None = None,
        require_credentials: bool = False,
        max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
    ) -> None:
        if max_image_bytes <= 0:
            raise ValueError("max_image_bytes must be positive")
        self.credentials = credentials
        self.require_credentials = require_credentials
        self.max_image_bytes = max_image_bytes
        self._tags: dict[tuple[str, str], str] = {}
        self._blobs: dict[tuple[str, str], bytes] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        *,
        repository: str,
        tag: str,
        digest: str,
        content: bytes,
        credential: str | None = None,
    ) -> str:
        validate_repository(repository)
        validate_tag(tag)
        validate_digest(digest)
        if not isinstance(content, (bytes, bytearray)):
            raise TypeError("image content must be bytes")
        payload = bytes(content)
        if self.credentials is not None:
            if credential is None:
                if self.require_credentials:
                    raise PushAccessDenied("push credential is required")
            else:
                self.credentials.verify_push(credential, repository=repository)
        elif self.require_credentials:
            raise PushAccessDenied("no credential verifier is configured")

        if len(payload) > self.max_image_bytes:
            raise ImageTooLarge(size_bytes=len(payload), limit_bytes=self.max_image_bytes)

        actual = digest_for(payload)
        if actual != digest:
            raise ImageDigestMismatch(
                repository=repository,
                tag=tag,
                local_digest=digest,
                remote_digest=actual,
            )

        async with self._lock:
            existing = self._tags.get((repository, tag))
            if existing is not None and existing != digest:
                raise ImmutableTagConflict(repository, tag)
            self._tags[(repository, tag)] = digest
            self._blobs.setdefault((repository, digest), payload)
        return digest

    async def pull(
        self,
        *,
        repository: str,
        reference: str,
        credential: str | None = None,
    ) -> bytes:
        validate_repository(repository)
        if self.credentials is not None:
            if credential is None:
                raise PullAccessDenied("pull credential is required")
            self.credentials.verify_pull(credential, repository=repository)
        elif self.require_credentials:
            raise PullAccessDenied("no credential verifier is configured")

        if reference.startswith("sha256:"):
            validate_digest(reference)
            digest = reference
        else:
            validate_tag(reference)
            digest = self._tags.get((repository, reference), "")
            if not digest:
                raise ContainerRegistryUnavailable(
                    f"image {repository}:{reference} is not present in the registry"
                )
        content = self._blobs.get((repository, digest))
        if content is None:
            raise ContainerRegistryUnavailable(
                f"image {repository}@{digest} is not present in the registry"
            )
        return content

    async def delete(self, *, repository: str, reference: str) -> bool:
        validate_repository(repository)
        async with self._lock:
            if reference.startswith("sha256:"):
                validate_digest(reference)
                digest = reference
                removed = self._blobs.pop((repository, digest), None) is not None
                for key, value in list(self._tags.items()):
                    if key[0] == repository and value == digest:
                        self._tags.pop(key, None)
                return removed
            validate_tag(reference)
            digest = self._tags.pop((repository, reference), None)
            if digest is None:
                return False
            if not any(
                repo == repository and value == digest
                for (repo, _tag), value in self._tags.items()
            ):
                self._blobs.pop((repository, digest), None)
            return True

    def digest_for_tag(self, repository: str, tag: str) -> str | None:
        return self._tags.get((repository, tag))

    def has_tag(self, repository: str, tag: str) -> bool:
        return (repository, tag) in self._tags
